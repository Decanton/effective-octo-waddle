"""More places to find jobs: Kenyan job sites, job blogs and extra remote boards.

Three kinds of source, all free and configured in config/search_queries.yaml:

* rss_feeds:   RSS/Atom feeds (most Kenyan job blogs run WordPress, whose /feed/ lists every new post
               with its full text, including "send your CV and cover letter to hr@...").
* html_boards: job sites without a feed (BrighterMonday, Fuzu, MyJobMag ...). The agent reads the
               listing page, opens each job and reads the JobPosting data the site publishes for
               Google Jobs (schema.org JSON-LD): title, company, place, exact posting date, full text.
* json_apis:   remote-job APIs (Arbeitnow, Working Nomads).

Every job still passes the 24-hour rule. A post dated only with today's date (no time) counts
as today; yesterday's does not.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests

from .common import USER_AGENT, html_to_text, log, new_job
from .freshness import parse_age_hours

NAIROBI = timezone(timedelta(hours=3))

DEFAULT_RSS = [
    {"name": "jobwebkenya", "url": "https://www.jobwebkenya.com/feed/", "location": "Kenya"},
    {"name": "corporatestaffing", "url": "https://www.corporatestaffing.co.ke/feed/", "location": "Kenya"},
    {"name": "careerpointkenya", "url": "https://www.careerpointkenya.co.ke/feed/", "location": "Kenya"},
    {"name": "opportunitiesforyoungkenyans", "url": "https://opportunitiesforyoungkenyans.co.ke/feed/", "location": "Kenya"},
    {"name": "ngojobsinafrica", "url": "https://ngojobsinafrica.com/feed/", "location": "Kenya"},
]
DEFAULT_HTML = [
    {"name": "brightermonday", "list_url": "https://www.brightermonday.co.ke/jobs?sort=latest", "link": r"/listings/[^\"'?#]+"},
    {"name": "fuzu", "list_url": "https://www.fuzu.com/kenya/job", "link": r"/kenya/job/[^\"'?#]+"},
    {"name": "myjobmag_web", "list_url": "https://www.myjobmag.co.ke/jobs", "link": r"/job/[^\"'?#]+"},
    {"name": "kenyajob", "list_url": "https://www.kenyajob.com/job-vacancies-kenya", "link": r"/job-vacancies-kenya/[^\"'?#]+"},
]
DEFAULT_JSON = ["arbeitnow", "workingnomads"]


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
                      "Accept-Language": "en-KE,en;q=0.9"})
    return s


def age_of(value) -> float | None:
    """Hours since posting. A bare date counts only if it is today in Nairobi."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        now = datetime.now(NAIROBI)
        if text != now.date().isoformat():
            return 99.0
        return (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
    try:
        return parse_age_hours(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        return parse_age_hours(text)


TITLE_AT = re.compile(r"^(?P<title>.+?)\s+(?:job\s+)?at\s+(?P<company>[^|–\-:]+?)(?:\s*[|–\-:].*)?$", re.I)
TITLE_COLON = re.compile(r"^(?P<company>[^:]{2,60}?)\s+(?:jobs?|vacancies|careers|hiring)\s*[:\-–]\s*(?P<title>.+)$", re.I)


def split_title(raw: str) -> tuple[str, str | None]:
    raw = " ".join((raw or "").split())
    for rx in (TITLE_COLON, TITLE_AT):
        m = rx.match(raw)
        if m:
            return m.group("title").strip(" -–"), m.group("company").strip()
    return raw, None


# ---------- RSS ----------

def parse_rss(xml_text: str, source: str, location: str = "Kenya") -> list[dict]:
    out = []
    root = ElementTree.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    ns = {"content": "http://purl.org/rss/1.0/modules/content/", "atom": "http://www.w3.org/2005/Atom"}
    items = list(root.iter("item")) or list(root.iter("{http://www.w3.org/2005/Atom}entry"))
    for it in items:
        def get(*tags):
            for t in tags:
                el = it.find(t, ns)
                if el is not None:
                    return (el.text or el.get("href") or "").strip()
            return ""
        link = get("link", "atom:link")
        body = get("content:encoded", "atom:content") or get("description", "atom:summary")
        title, company = split_title(get("title", "atom:title"))
        pub = get("pubDate", "atom:published", "atom:updated")
        if not link or not title:
            continue
        text = html_to_text(body)
        out.append(new_job(source=source, url=link, title=title, company=company,
                           location=_place(text) or location, is_remote=bool(re.search(r"\bremote\b", title + " " + text[:400], re.I)) or None,
                           description=text, posted_at=pub, age_hours=age_of(pub)))
    return out


def _place(text: str) -> str | None:
    m = re.search(r"\b(?:location|duty station|job location)\s*[:\-]\s*([A-Za-z ,]{3,40})", text or "", re.I)
    if not m:
        return None
    place = m.group(1).strip(" ,")
    return place if re.search(r"kenya|nairobi|mombasa|kisumu|nakuru|eldoret|thika|remote", place, re.I) else f"{place}, Kenya"


# ---------- JSON-LD job pages ----------

def jobposting(html: str) -> dict | None:
    """The schema.org JobPosting a page publishes for Google Jobs."""
    for raw in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html or "", re.S | re.I):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                t = d.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    return d
                stack += [v for v in d.values() if isinstance(v, (dict, list))]
            elif isinstance(d, list):
                stack += d
    return None


def _ld_location(d: dict) -> tuple[str, bool]:
    remote = str(d.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    locs = d.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    parts = []
    for loc in locs:
        a = (loc or {}).get("address") or {}
        if isinstance(a, str):
            parts.append(a)
        else:
            parts.append(", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"),
                                               (a.get("addressCountry") or {}).get("name") if isinstance(a.get("addressCountry"), dict) else a.get("addressCountry")) if x))
    place = "; ".join(p for p in parts if p) or ("Remote" if remote else "Kenya")
    return place, remote


def job_from_page(url: str, html: str, source: str) -> dict | None:
    d = jobposting(html)
    if not d:
        return None
    org = d.get("hiringOrganization") or {}
    company = org.get("name") if isinstance(org, dict) else str(org)
    place, remote = _ld_location(d)
    apply = d.get("url") if isinstance(d.get("url"), str) else None
    body = html_to_text(d.get("description")) + "\n" + _how_to_apply(html)
    return new_job(source=source, url=url, apply_url=apply if apply and apply != url else url, title=html_to_text(d.get("title")),
                   company=company, location=place, is_remote=remote or None, description=body.strip(),
                   posted_at=d.get("datePosted"), age_hours=age_of(d.get("datePosted")))


def _how_to_apply(html: str) -> str:
    """The 'How to apply' part of the page, which often holds the email address."""
    text = html_to_text(html)
    m = re.search(r"(how to apply|method of application|application (process|procedure)|to apply)[\s\S]{0,900}", text, re.I)
    return m.group(0) if m else ""


def fetch_html_board(sess, board: dict, max_jobs: int = 40, pause: float = 1.0, timeout: float = 25) -> list[dict]:
    r = sess.get(board["list_url"], timeout=timeout)
    r.raise_for_status()
    seen, links = set(), []
    for m in re.finditer(r"href=[\"']([^\"']+)[\"']", r.text):
        href = urljoin(board["list_url"], m.group(1)).split("#")[0].split("?")[0]
        if re.search(board["link"], urlparse(href).path) and href not in seen:
            seen.add(href)
            links.append(href)
    out = []
    for href in links[:max_jobs]:
        try:
            page = sess.get(href, timeout=timeout)
            if page.ok:
                job = job_from_page(href, page.text, board["name"])
                if job and job["title"]:
                    out.append(job)
                    if (job["age_hours"] or 0) > 48 and len(out) > 10:
                        break  # newest first; the rest are older
        except requests.RequestException:
            continue
        time.sleep(pause)
    return out


# ---------- JSON APIs ----------

def parse_arbeitnow(data: dict) -> list[dict]:
    return [new_job(source="arbeitnow", url=j.get("url"), title=j.get("title"), company=j.get("company_name"),
                    location=j.get("location") or ("Remote" if j.get("remote") else None), is_remote=bool(j.get("remote")),
                    description=html_to_text(j.get("description")), posted_at=str(j.get("created_at")),
                    age_hours=parse_age_hours(j.get("created_at")))
            for j in (data or {}).get("data", []) if j.get("remote")]


def parse_workingnomads(data: list) -> list[dict]:
    return [new_job(source="workingnomads", url=j.get("url"), title=j.get("title"), company=j.get("company_name"),
                    location=j.get("location") or "Remote", is_remote=True, description=html_to_text(j.get("description")),
                    posted_at=j.get("pub_date"), age_hours=parse_age_hours(j.get("pub_date")))
            for j in data or [] if isinstance(j, dict)]


JSON_APIS = {
    "arbeitnow": ("https://www.arbeitnow.com/api/job-board-api", parse_arbeitnow),
    "workingnomads": ("https://www.workingnomads.com/api/exposed_jobs/", parse_workingnomads),
}


def fetch(cfg: dict, report: dict | None = None) -> list[dict]:
    """All extra sources. A source that fails is logged and skipped. report[name] = (found, fresh, error)."""
    sess, jobs = _session(), []
    report = report if report is not None else {}
    more = cfg.get("more_sources") or {}
    if more.get("enabled", True) is False:
        return []

    def note(name, found, err=None):
        fresh = sum(1 for j in found if j["age_hours"] is not None and j["age_hours"] <= 24)
        report[name] = (len(found), fresh, err)
        if err:
            log.warning("Source %s failed: %s", name, err)
        else:
            log.info("Source %s: %d postings (%d from the last 24h)", name, len(found), fresh)

    for f in more.get("rss_feeds", DEFAULT_RSS):
        try:
            r = sess.get(f["url"], timeout=25)
            r.raise_for_status()
            found = parse_rss(r.text, f["name"], f.get("location", "Kenya"))
            note(f["name"], found)
            jobs += found
        except Exception as exc:
            note(f["name"], [], str(exc)[:160])
    for b in more.get("html_boards", DEFAULT_HTML):
        try:
            found = fetch_html_board(sess, b, int(more.get("max_jobs_per_board", 40)), float(more.get("pause_seconds", 1.0)))
            note(b["name"], found)
            jobs += found
        except Exception as exc:
            note(b["name"], [], str(exc)[:160])
    for name in more.get("json_apis", DEFAULT_JSON):
        url, parse = JSON_APIS.get(name, (None, None))
        if not url:
            continue
        try:
            r = sess.get(url, timeout=25)
            r.raise_for_status()
            found = parse(r.json())
            note(name, found)
            jobs += found
        except Exception as exc:
            note(name, [], str(exc)[:160])
    return jobs
