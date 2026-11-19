"""LinkedIn job search inside Job Agent Chrome, where you are signed in to LinkedIn.

LinkedIn blocks anonymous searches after a few requests (HTTP 429), so the public search
finds little. Signed in, the agent searches the way you would: Easy Apply jobs from the last
48 hours, in Kenya and remote worldwide, for each search in search_queries.yaml. It then
opens the best-looking posts to read their full text for ranking and CV tailoring.

It is human-paced and capped (linkedin_search in config/agent_settings.yaml). It never
solves a LinkedIn security check: if one appears, the search stops for this run.
"""
from __future__ import annotations

import os
import random
import re
import time
from urllib.parse import quote_plus

from .common import log, new_job

KENYA_GEO = "100710459"
WORLDWIDE_GEO = "92000000"
CARD = "li[data-occludable-job-id], div.job-card-container[data-job-id], li.jobs-search-results__list-item"
DESC = ".jobs-description__content, #job-details, .jobs-box__html-content, .jobs-description-content__text"


def search_url(term: str, geo: str, remote: bool, hours: int = 48, start: int = 0) -> str:
    url = (f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(term)}&geoId={geo}"
           f"&f_AL=true&f_TPR=r{int(hours) * 3600}&sortBy=DD")
    if remote:
        url += "&f_WT=2"
    return url + (f"&start={start}" if start else "")


def _plain_term(term: str) -> str:
    """LinkedIn's box takes OR, but long boolean strings find little; keep the first three phrases."""
    parts = [p.strip().strip('"') for p in re.split(r"\s+OR\s+", term) if p.strip()]
    return " OR ".join(f'"{p}"' for p in parts[:3]) if parts else term


def _pause(lo=2.5, hi=5.5):
    if not os.environ.get("JOBSPRINT_FAST"):
        time.sleep(random.uniform(lo, hi))


def cards(page) -> list[dict]:
    """Job cards on a LinkedIn search results page."""
    return page.evaluate(r"""
    () => [...document.querySelectorAll('a[href*="/jobs/view/"]')].map(a => {
      const m = a.href.match(/\/jobs\/view\/(?:[^\/]*-)?(\d{6,})/); if (!m) return null;
      const card = a.closest('li, .job-card-container, [data-job-id]') || a.parentElement;
      const txt = s => { const e = card && card.querySelector(s); return e ? e.innerText.replace(/\s+/g, ' ').trim() : ''; };
      const title = (a.getAttribute('aria-label') || a.innerText || '').replace(/\s+/g, ' ').replace(/ with verification$/i, '').trim();
      const time = card && card.querySelector('time');
      return { id: m[1], title,
               company: txt('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, .job-card-container__company-name'),
               location: txt('.artdeco-entity-lockup__caption, .job-card-container__metadata-item, .job-card-container__metadata-wrapper li'),
               posted: time ? time.getAttribute('datetime') : null,
               applied: /\bapplied\b/i.test(card ? card.innerText : '') };
    }).filter(Boolean)
    """)


def _age(posted: str | None) -> float:
    from .more_boards import age_of
    a = age_of(posted) if posted else None
    return a if a is not None else 24.0  # the search itself only returns the last 48 hours


def fetch(ws, search_cfg: dict, cfg: dict) -> list[dict]:
    """Search LinkedIn signed in. Returns new_job records (with full text for the top ones)."""
    if os.environ.get("JOBSPRINT_HEADLESS") == "1" or not cfg.get("enabled", True):
        return []
    from ..automation import linkedin_easy_apply as lea
    from ..automation.browser_manager import browser
    hours = int(search_cfg.get("max_age_hours", 48))
    max_pages, max_details = int(cfg.get("pages_per_search", 1)), int(cfg.get("max_details", 60))
    found: dict[str, dict] = {}
    with browser(ws) as ctx:
        page = ctx.new_page()
        try:
            if not lea.ensure_signed_in(page):
                log.info("LinkedIn search skipped: not signed in to LinkedIn in Job Agent Chrome.")
                return []
            for s in search_cfg.get("searches", []):
                for geo, remote, place in ((KENYA_GEO, False, "Kenya"), (WORLDWIDE_GEO, True, "Remote")):
                    for p in range(max_pages):
                        page.goto(search_url(_plain_term(s["term"]), geo, remote, hours, p * 25), wait_until="domcontentloaded")
                        page.wait_for_timeout(3000)
                        if lea.CHECKPOINT.search(page.url):
                            log.warning("LinkedIn showed a security check; stopping the LinkedIn search for this run.")
                            return list(found.values())
                        got = [c for c in cards(page) if not c["applied"]]
                        for c in got:
                            if c["id"] not in found:
                                found[c["id"]] = new_job(
                                    source="linkedin_browser", external_id=c["id"], url=f"https://www.linkedin.com/jobs/view/{c['id']}/",
                                    title=c["title"], company=c["company"] or None, location=c["location"] or place,
                                    is_remote=remote or None, description="", posted_at=c["posted"], age_hours=_age(c["posted"]),
                                    track=s.get("track"))
                        _pause()
                        if len(got) < 20:
                            break
            log.info("LinkedIn search (signed in): %d Easy Apply jobs from the last %dh", len(found), hours)
            # Full text for the most promising ones, so ranking and CV tailoring have something to read.
            excl = [w.lower() for w in search_cfg.get("title_exclude") or []]
            todo = [j for j in found.values() if not any(w in j["title"].lower() for w in excl)]
            for j in todo[:max_details]:
                try:
                    page.goto(j["url"], wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    if lea.CHECKPOINT.search(page.url):
                        break
                    el = page.locator(DESC).first
                    if el.count():
                        j["description"] = el.inner_text(timeout=5000)
                except Exception:
                    continue
                _pause(2.0, 4.0)
        finally:
            try:
                page.close()
            except Exception:
                pass
    return list(found.values())
