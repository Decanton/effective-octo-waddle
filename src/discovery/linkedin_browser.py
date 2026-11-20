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
    () => {
      const seen = new Set(), out = [];
      const ids = el => { const a = el.querySelector && el.querySelector('a[href*="/jobs/view/"], a[href*="currentJobId="]');
        const m = a && (a.href.match(/\/jobs\/view\/(?:[^\/]*-)?(\d{6,})/) || a.href.match(/currentJobId=(\d{6,})/));
        return [el.getAttribute('data-occludable-job-id') || el.getAttribute('data-job-id') || (m && m[1]), a]; };
      const roots = [...document.querySelectorAll('[data-occludable-job-id], [data-job-id]')];
      for (const a of document.querySelectorAll('a[href*="/jobs/view/"], a[href*="currentJobId="]'))
        if (!a.closest('[data-occludable-job-id], [data-job-id]')) roots.push(a.closest('li') || a);
      for (const card of roots) {
        const [id, a] = ids(card); if (!id || seen.has(id) || !/^\d{6,}$/.test(id)) continue; seen.add(id);
        out.push([id, a, card]);
      }
      return out.map(([id, a, card]) => {
      const txt = s => { const e = card && card.querySelector(s); return e ? e.innerText.replace(/\s+/g, ' ').trim() : ''; };
      const tEl = a || card.querySelector('strong, .job-card-list__title, .artdeco-entity-lockup__title');
      const title = ((a && a.getAttribute('aria-label')) || (tEl && tEl.innerText) || '').split('\n')[0].replace(/\s+/g, ' ').replace(/ with verification$/i, '').trim();
      const time = card && card.querySelector('time');
      return { id, title,
               company: txt('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, .job-card-container__company-name'),
               location: txt('.artdeco-entity-lockup__caption, .job-card-container__metadata-item, .job-card-container__metadata-wrapper li'),
               posted: time ? time.getAttribute('datetime') : null,
               applied: /\bapplied\b/i.test(card ? card.innerText : '') };
      }).filter(c => c.title);
    }
    """)


RESULTS = 'a[href*="/jobs/view/"], a[href*="currentJobId="], [data-occludable-job-id], [data-job-id]'
NO_RESULTS = '.jobs-search-no-results-banner, .jobs-search-two-pane__no-results-banner--expand'


def _render_all(page) -> None:
    """LinkedIn draws a card's text only once it's scrolled into view: scroll the result list down."""
    page.evaluate("""async () => {
      const first = document.querySelector('[data-occludable-job-id], [data-job-id]');
      let box = first; while (box && box !== document.body && !(box.scrollHeight > box.clientHeight + 20 &&
             /(auto|scroll)/.test(getComputedStyle(box).overflowY))) box = box.parentElement;
      box = box && box !== document.body ? box : document.scrollingElement;
      for (let y = 0; y < box.scrollHeight; y += 400) { box.scrollTop = y; await new Promise(r => setTimeout(r, 250)); }
    }""")


GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def guest_url(term: str, geo: str, remote: bool, hours: int = 48, start: int = 0) -> str:
    return search_url(term, geo, remote, hours, start).replace("https://www.linkedin.com/jobs/search/", GUEST_API)


def parse_guest(html: str) -> list[dict]:
    """Cards from LinkedIn's lightweight results list (the same list its own page loads as you scroll)."""
    from bs4 import BeautifulSoup
    out = []
    for li in BeautifulSoup(html or "", "html.parser").select("li"):
        card = li.select_one("[data-entity-urn]")
        m = re.search(r"jobPosting:(\d{6,})", card.get("data-entity-urn", "")) if card else None
        if not m:
            a = li.select_one('a[href*="/jobs/view/"]')
            m = re.search(r"/jobs/view/(?:[^/?]*-)?(\d{6,})", a.get("href", "")) if a else None
        title = li.select_one(".base-search-card__title, h3")
        if not (m and title):
            continue
        sub, loc, t = li.select_one(".base-search-card__subtitle, h4"), li.select_one(".job-search-card__location"), li.select_one("time")
        out.append({"id": m.group(1), "title": " ".join(title.get_text().split()),
                    "company": " ".join(sub.get_text().split()) if sub else "", "location": " ".join(loc.get_text().split()) if loc else "",
                    "posted": t.get("datetime") if t else None, "applied": False})
    return out


def _guest_cards(page, term, geo, remote, hours, start) -> list[dict] | None:
    """The results list fetched directly with Job Agent Chrome's cookies, for when the full search page is too slow."""
    try:
        r = page.request.get(guest_url(term, geo, remote, hours, start), timeout=30000)
    except Exception as exc:
        log.info("LinkedIn results list didn't load either: %s", str(exc)[:80])
        return None
    if r.status == 429:
        log.info("LinkedIn is rate-limiting the results list (429); stopping the search for this run.")
        return None
    return parse_guest(r.text()) if r.ok else []


def _debug_dump(ws, page, name: str) -> None:
    try:
        base = ws.logs / f"linkedin_{name}"
        page.screenshot(path=str(base) + ".png", timeout=10000)
        (base.parent / (base.name + ".txt")).write_text(f"{page.url}\n\n{page.inner_text('body', timeout=5000)[:20000]}", encoding="utf-8")
    except Exception:
        pass


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
    # LinkedIn's new search page (/jobs/search-results/) has no readable job cards and its semantic
    # search ignores most filters, so by default the agent reads LinkedIn's results list directly.
    slow, use_list = 0, bool(cfg.get("use_results_list", True))
    with browser(ws) as ctx:
        page = ctx.new_page()
        try:
            if not lea.ensure_signed_in(page):
                log.info("LinkedIn search skipped: not signed in to LinkedIn in Job Agent Chrome.")
                return []
            for s in search_cfg.get("searches", []):
                for geo, remote, place in ((KENYA_GEO, False, "Kenya"), (WORLDWIDE_GEO, True, "Remote")):
                    for p in range(max_pages):
                        term = _plain_term(s["term"])
                        got = None
                        if not use_list:
                            try:  # LinkedIn can be slow; one slow page mustn't end the whole search
                                page.goto(search_url(term, geo, remote, hours, p * 25), wait_until="domcontentloaded", timeout=60000)
                                page.wait_for_selector(f"{RESULTS}, {NO_RESULTS}", timeout=30000)
                                page.wait_for_timeout(1500)
                                if page.locator(NO_RESULTS).count():
                                    break  # no Easy Apply jobs for this search in the last 48 hours
                                _render_all(page)
                                got = [c for c in cards(page) if not c["applied"]]
                            except Exception as exc:
                                log.info("LinkedIn search page slow or unreadable (%s): %s", s["term"][:40], str(exc)[:80])
                            if lea.CHECKPOINT.search(page.url or ""):
                                log.warning("LinkedIn showed a security check; stopping the LinkedIn search for this run.")
                                return list(found.values())
                            if not got:
                                slow += 1
                                if slow == 1:
                                    _debug_dump(ws, page, "search_page")  # what the page showed, for fixing it
                                if slow >= 2:  # the full page keeps failing: use the results list from now on
                                    use_list = True
                                    log.info("LinkedIn search pages keep failing; reading LinkedIn's results list instead.")
                        if got is None or (not got and use_list):
                            got = _guest_cards(page, term, geo, remote, hours, p * 25)
                            if got is None:
                                return list(found.values())
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
                if use_list:  # the light version of the job page; the full one was too slow today
                    try:
                        r = page.request.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{j['external_id']}", timeout=30000)
                        if r.ok:
                            from bs4 import BeautifulSoup
                            d = BeautifulSoup(r.text(), "html.parser").select_one(".show-more-less-html__markup, .description__text")
                            j["description"] = d.get_text("\n", strip=True) if d else ""
                        elif r.status == 429:
                            break
                    except Exception:
                        pass
                    _pause(1.5, 3.0)
                    continue
                try:
                    page.goto(j["url"], wait_until="domcontentloaded", timeout=60000)
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
