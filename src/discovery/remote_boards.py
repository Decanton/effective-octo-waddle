"""Free remote-job boards with public feeds and exact posting times.

These list many roles open worldwide, which suits a candidate in Kenya:
  Remotive        https://remotive.com/api/remote-jobs        publication_date
  RemoteOK        https://remoteok.com/api                    epoch
  Himalayas       https://himalayas.app/jobs/api              pubDate (epoch seconds)
  Jobicy          https://jobicy.com/api/v2/remote-jobs       pubDate
  We Work Remotely https://weworkremotely.com/remote-jobs.rss pubDate (RSS)
Every job still has to pass the 24-hour rule and the location rules in the ranker.
"""
from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

from .common import USER_AGENT, html_to_text, log, new_job
from .freshness import parse_age_hours

FEEDS = {
    "remotive": "https://remotive.com/api/remote-jobs?limit=300",
    "remoteok": "https://remoteok.com/api",
    "himalayas": "https://himalayas.app/jobs/api?limit=100&offset={offset}",
    "jobicy": "https://jobicy.com/api/v2/remote-jobs?count=100",
    "weworkremotely": "https://weworkremotely.com/remote-jobs.rss",
}


def _utc(text):
    """Timestamps without a zone from these feeds are UTC."""
    if isinstance(text, str) and text and "T" not in text and ":" in text:
        text = text.replace(" ", "T")
    if isinstance(text, str) and "T" in text and not text.endswith("Z") and "+" not in text[10:]:
        text += "+00:00"
    return text


def parse_remotive(data: dict) -> list[dict]:
    return [new_job(source="remotive", external_id=str(j.get("id")), url=j.get("url"), title=j.get("title"),
                    company=j.get("company_name"), location=j.get("candidate_required_location") or "Remote",
                    is_remote=True, description=html_to_text(j.get("description")),
                    posted_at=j.get("publication_date"), age_hours=parse_age_hours(_utc(j.get("publication_date"))))
            for j in data.get("jobs", [])]


def parse_remoteok(data: list) -> list[dict]:
    out = []
    for j in data or []:
        if not isinstance(j, dict) or not j.get("position"):
            continue  # first element is a legal notice
        out.append(new_job(source="remoteok", external_id=str(j.get("id")), url=j.get("url"),
                           apply_url=j.get("apply_url") or j.get("url"), title=j.get("position"),
                           company=j.get("company"), location=j.get("location") or "Remote", is_remote=True,
                           description=html_to_text(j.get("description")) + " " + " ".join(j.get("tags") or []),
                           posted_at=j.get("date"), age_hours=parse_age_hours(j.get("epoch") or j.get("date"))))
    return out


def parse_himalayas(data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        restr = j.get("locationRestrictions") or []
        loc = ", ".join(r if isinstance(r, str) else (r.get("name") or "") for r in restr) or "Worldwide"
        pub = j.get("pubDate")
        out.append(new_job(source="himalayas", external_id=str(j.get("guid") or j.get("id")),
                           url=j.get("applicationLink") or j.get("guid"), title=j.get("title"),
                           company=j.get("companyName"), location=loc, is_remote=True,
                           description=html_to_text(j.get("description") or j.get("excerpt")),
                           posted_at=str(pub), age_hours=parse_age_hours(pub)))
    return out


def parse_jobicy(data: dict) -> list[dict]:
    return [new_job(source="jobicy", external_id=str(j.get("id")), url=j.get("url"), title=j.get("jobTitle"),
                    company=j.get("companyName"), location=j.get("jobGeo") or "Remote", is_remote=True,
                    description=html_to_text(j.get("jobDescription") or j.get("jobExcerpt")),
                    posted_at=j.get("pubDate"), age_hours=parse_age_hours(_utc(j.get("pubDate"))))
            for j in data.get("jobs", [])]


def parse_wwr(xml_text: str) -> list[dict]:
    out = []
    root = ElementTree.fromstring(xml_text)
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
        title = get("title")
        company, _, role = title.partition(": ")
        try:
            posted = parsedate_to_datetime(get("pubDate"))
        except (TypeError, ValueError):
            posted = None
        out.append(new_job(source="weworkremotely", url=get("link") or get("guid"), title=role or title,
                           company=company if role else None, location=get("region") or "Remote", is_remote=True,
                           description=html_to_text(get("description")),
                           posted_at=posted.isoformat() if posted else None, age_hours=parse_age_hours(posted)))
    return out


def fetch(boards=("remotive", "remoteok", "himalayas", "jobicy"), timeout: float = 25) -> list[dict]:
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml, */*"})
    jobs = []
    for name in boards:
        try:
            if name == "himalayas":
                found = []
                for page in range(3):  # newest first; 300 is plenty for one day
                    r = sess.get(FEEDS[name].format(offset=page * 100), timeout=timeout)
                    r.raise_for_status()
                    batch = parse_himalayas(r.json())
                    found += batch
                    if not batch or all((j["age_hours"] or 99) > 48 for j in batch):
                        break
                    time.sleep(1)
            else:
                r = sess.get(FEEDS[name], timeout=timeout)
                r.raise_for_status()
                found = {"remotive": lambda: parse_remotive(r.json()), "remoteok": lambda: parse_remoteok(r.json()),
                         "jobicy": lambda: parse_jobicy(r.json()), "weworkremotely": lambda: parse_wwr(r.text)}[name]()
        except (requests.RequestException, ValueError, ElementTree.ParseError) as exc:
            log.warning("Remote board %s failed: %s", name, exc)
            continue
        fresh = sum(1 for j in found if j["age_hours"] is not None and j["age_hours"] <= 48)
        log.info("Remote board %s: %d postings (%d from the last 48h)", name, len(found), fresh)
        jobs += found
    return jobs
