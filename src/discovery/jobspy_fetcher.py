"""Fetches jobs through python-jobspy (LinkedIn by default) and enforces the 24-hour rule.

LinkedIn search results only carry a posting date, not a time. Searches ask
LinkedIn for the last 24 hours only (its f_TPR filter), and for the best
matches the agent then opens the public job page to read the exact age
("3 hours ago") and the applicant count ("Over 200 applicants").
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from .common import USER_AGENT, load_yaml, log, new_job
from .freshness import parse_age_hours, parse_applicants

LINKEDIN_JOB_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"


def _val(row, key):
    v = row.get(key)
    try:
        import pandas as pd
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def fetch(cfg: dict | None = None, max_age: float | None = None) -> list[dict]:
    try:
        from jobspy import scrape_jobs
    except Exception as exc:  # show the real reason, not a guess
        log.error("Could not load python-jobspy, so LinkedIn was skipped: %s: %s", type(exc).__name__, exc)
        return []
    cfg = cfg or load_yaml("search_queries.yaml")
    max_age = max_age or float(cfg.get("max_age_hours", 24))
    jcfg = cfg.get("jobspy", {})
    sites = jcfg.get("sites", ["linkedin"])
    wanted = int(jcfg.get("results_per_search", 25))
    pause = float(jcfg.get("request_pause_seconds", 2.0))
    hours = int(max_age)

    jobs, seen = [], set()
    for s in cfg.get("searches", []):
        for loc in cfg.get("locations", []):
            try:
                df = scrape_jobs(site_name=sites, search_term=s["term"],
                                 google_search_term=f"{s['term']} jobs {loc['location']} since yesterday",
                                 location=loc["location"], is_remote=bool(loc.get("remote_only")),
                                 results_wanted=wanted, hours_old=hours, verbose=0)
            except Exception as exc:  # one failing search must not stop the sprint
                log.warning("jobspy search '%s' in %s failed: %s", s["term"], loc["location"], exc)
                time.sleep(pause)
                continue
            for row in df.to_dict("records"):
                url = _val(row, "job_url")
                if not url or url in seen:
                    continue
                seen.add(url)
                posted = _val(row, "date_posted")
                if isinstance(posted, datetime):
                    posted_val = posted
                elif isinstance(posted, date):
                    posted_val = posted
                else:
                    posted_val = str(posted) if posted else None
                # The search itself was limited to `hours` on the site's side,
                # so a bare date of today or yesterday is accepted for now.
                age = parse_age_hours(posted_val, date_only_ok=True)
                jobs.append(new_job(
                    source=f"jobspy:{_val(row, 'site') or 'unknown'}",
                    external_id=str(_val(row, "id") or ""),
                    url=url, apply_url=_val(row, "job_url_direct") or url,
                    title=_val(row, "title"), company=_val(row, "company"),
                    location=_val(row, "location"), is_remote=_val(row, "is_remote"),
                    description=_val(row, "description"),
                    posted_at=str(posted_val) if posted_val else None, age_hours=age,
                    applicant_count=parse_applicants(_val(row, "num_applicants") or _val(row, "applicants")),
                    track=s.get("track")))
            log.info("jobspy '%s' in %s: %d jobs so far", s["term"], loc["location"], len(jobs))
            time.sleep(pause)
    return jobs  # the ranker applies the 24-hour rule to every source the same way


def linkedin_details(job: dict, session: requests.Session | None = None, timeout: float = 15) -> dict:
    """Read exact posting age, applicant count and description from LinkedIn's public job page."""
    m = re.search(r"(\d{6,})", job.get("external_id") or job["url"])
    if not m:
        return job
    sess = session or requests.Session()
    try:
        r = sess.get(LINKEDIN_JOB_API.format(id=m.group(1)), headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if r.status_code != 200:
            return job
    except requests.RequestException:
        return job
    return {**job, **parse_linkedin_job_html(r.text, job)}


def parse_linkedin_job_html(html: str, job: dict | None = None) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    t = soup.select_one(".posted-time-ago__text") or soup.find(string=re.compile(r"\bago\b"))
    if t is not None:
        txt = t.get_text(" ", strip=True) if hasattr(t, "get_text") else str(t)
        age = parse_age_hours(txt)
        if age is not None:
            out["age_hours"] = age
    a = soup.select_one(".num-applicants__caption") or soup.find(string=re.compile(r"applicants", re.I))
    if a is not None:
        n = parse_applicants(a.get_text(" ", strip=True) if hasattr(a, "get_text") else str(a))
        if n is not None:
            out["applicant_count"] = n
    if not (job or {}).get("description"):
        d = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
        if d is not None:
            out["description"] = d.get_text("\n", strip=True)
    return out


def enrich_linkedin(jobs: list[dict], top_n: int, pause: float = 2.0) -> list[dict]:
    """Fetch details for the first top_n LinkedIn jobs (call after a first ranking)."""
    sess = requests.Session()
    done = 0
    out = []
    for j in jobs:
        if done < top_n and "linkedin" in (j.get("source") or ""):
            j = linkedin_details(j, sess)
            done += 1
            time.sleep(pause)
        out.append(j)
    if done:
        log.info("Read applicant counts and exact ages for %d LinkedIn jobs", done)
    return out
