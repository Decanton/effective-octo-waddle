"""Fetches postings from public Greenhouse, Lever and Ashby job boards.

These boards publish exact posting timestamps, so the 24-hour rule is exact:
  Greenhouse: first_published  (updated_at is NOT used: an edit is not a new post)
  Lever:      createdAt (epoch milliseconds)
  Ashby:      publishedAt
"""
from __future__ import annotations

import requests

from .common import USER_AGENT, html_to_text, load_yaml, log, new_job
from .freshness import parse_age_hours

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false",
}


def parse_greenhouse(data: dict, company: str, board: str) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        posted = j.get("first_published")
        out.append(new_job(source=f"greenhouse:{board}", external_id=str(j.get("id")),
                           url=j.get("absolute_url"), title=j.get("title"), company=company,
                           location=(j.get("location") or {}).get("name"),
                           description=html_to_text(j.get("content")),
                           posted_at=posted, age_hours=parse_age_hours(posted)))
    return out


def parse_lever(data: list, company: str, board: str) -> list[dict]:
    out = []
    for j in data or []:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        remote = (j.get("workplaceType") or "").lower() == "remote" or None
        desc = j.get("descriptionPlain") or html_to_text(j.get("description"))
        for lst in j.get("lists") or []:
            desc += "\n" + lst.get("text", "") + "\n" + html_to_text(lst.get("content"))
        out.append(new_job(source=f"lever:{board}", external_id=j.get("id"), url=j.get("hostedUrl"),
                           apply_url=j.get("applyUrl"), title=j.get("text"), company=company,
                           location=loc, is_remote=remote, description=desc,
                           posted_at=str(created) if created else None, age_hours=parse_age_hours(created)))
    return out


def parse_ashby(data: dict, company: str, board: str) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        posted = j.get("publishedAt")
        loc = j.get("location") or ""
        extra = [a.get("location") for a in j.get("secondaryLocations") or [] if a.get("location")]
        out.append(new_job(source=f"ashby:{board}", external_id=j.get("id"), url=j.get("jobUrl"),
                           apply_url=j.get("applyUrl"), title=j.get("title"), company=company,
                           location=", ".join([loc, *extra]) if extra else loc,
                           is_remote=j.get("isRemote") or (j.get("workplaceType") or "").lower() == "remote" or None,
                           description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                           posted_at=posted, age_hours=parse_age_hours(posted)))
    return out


PARSERS = {"greenhouse": parse_greenhouse, "lever": parse_lever, "ashby": parse_ashby}


def fetch(cfg: dict | None = None, timeout: float = 20) -> list[dict]:
    cfg = cfg or load_yaml("ai_platforms.yaml")
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    jobs = []
    for b in cfg.get("ats_boards", []):
        kind, board, name = b["kind"], b["board"], b.get("name", b["board"])
        try:
            r = sess.get(URLS[kind].format(board=board), timeout=timeout)
        except requests.RequestException as exc:
            log.warning("%s board '%s' could not be reached: %s", kind, board, exc)
            continue
        if r.status_code == 404:
            log.warning("%s board '%s' (%s) was not found. Fix or remove it in config/ai_platforms.yaml.", kind, board, name)
            continue
        if r.status_code != 200:
            log.warning("%s board '%s' answered HTTP %s", kind, board, r.status_code)
            continue
        try:
            found = PARSERS[kind](r.json(), name, board)
        except ValueError:
            log.warning("%s board '%s' returned something that isn't JSON", kind, board)
            continue
        log.info("%s board %s: %d postings", kind, name, len(found))
        jobs.extend(found)
    return jobs
