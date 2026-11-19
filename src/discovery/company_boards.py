"""Find a job on the company's own Greenhouse, Lever or Ashby board.

Job boards (Jobicy, Himalayas, We Work Remotely, LinkedIn) often hide the employer's apply
link behind their own sign-up or a paywall. Most tech companies post the same role on their
own application system, which the agent can fill and send unattended. This looks it up by
company name through the systems' free public APIs and matches the title.
"""
from __future__ import annotations

import logging
import re

import requests

from .ats_api_fetcher import PARSERS, URLS as API
from .common import USER_AGENT, load_yaml

log = logging.getLogger("jobsprint")
_cache: dict[tuple[str, str], list[dict] | None] = {}
NOISE = {"senior", "sr", "junior", "jr", "lead", "staff", "principal", "remote", "the", "and", "of", "for", "a", "an", "to", "in",
         "i", "ii", "iii", "iv", "contract", "full", "time", "part", "f", "m", "d", "x", "w", "global", "worldwide"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#]+", (s or "").lower()) if w not in NOISE}


def title_match(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def slugs(company: str) -> list[str]:
    name = re.sub(r"\b(inc|ltd|llc|limited|gmbh|plc|corp|corporation|co|technologies|labs?)\b\.?", " ", (company or "").lower())
    words = re.findall(r"[a-z0-9]+", name)
    full = re.findall(r"[a-z0-9]+", (company or "").lower())
    out = []
    for s in ("".join(full), "".join(words), "-".join(words), "-".join(full), words[0] if words else ""):
        if s and len(s) > 2 and s not in out:
            out.append(s)
    return out[:4]


def _known_boards(company: str) -> list[tuple[str, str]]:
    try:
        boards = load_yaml("ai_platforms.yaml").get("ats_boards") or []
    except Exception:
        return []
    key = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    return [(b["kind"], b["board"]) for b in boards if b.get("kind") in API
            and re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", b.get("name") or "").lower()) == key]


def _board(sess, ats: str, slug: str, company: str) -> list[dict] | None:
    if (ats, slug) in _cache:
        return _cache[(ats, slug)]
    jobs = None
    try:
        r = sess.get(API[ats].format(board=slug), timeout=10)
        if r.ok:
            jobs = PARSERS[ats](r.json(), company, slug)
    except (requests.RequestException, ValueError):
        jobs = None
    _cache[(ats, slug)] = jobs
    return jobs


def apply_link(ats: str, job: dict) -> str | None:
    if ats == "greenhouse":
        return job.get("url")
    if ats == "lever":
        u = job.get("apply_url") or job.get("url")
        return u if not u or u.endswith("/apply") else u.rstrip("/") + "/apply"
    if ats == "ashby":
        u = job.get("apply_url") if job.get("apply_url") and job.get("apply_url") != job.get("url") else job.get("url")
        return u if not u or u.endswith("/application") else u.rstrip("/") + "/application"
    return None


def find(job: dict, min_score: float = 0.6) -> str | None:
    """The application URL for this job on the company's own board, or None."""
    company = job.get("company")
    if not company or not job.get("title"):
        return None
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    tries = _known_boards(company) + [(ats, s) for s in slugs(company) for ats in ("greenhouse", "lever", "ashby")]
    best = (0.0, None)
    for ats, slug in tries:
        for j in _board(sess, ats, slug, company) or []:
            score = title_match(job["title"], j.get("title") or "")
            if score > best[0]:
                best = (score, apply_link(ats, j))
        if best[0] >= 0.99:
            break
    if best[0] >= min_score and best[1]:
        log.info("  found %s on %s's own application site: %s", job["title"][:50], company, best[1])
        return best[1]
    return None
