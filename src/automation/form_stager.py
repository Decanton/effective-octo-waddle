"""Stages applications: tailored CV, filled form, screenshot, then STOP before Submit.

For each top-ranked job:
  1. Build a CV tailored to the job from the single master template.
  2. Find the real application form (Greenhouse, Lever or Ashby, including
     ones linked from remote job boards).
  3. Fill every question it can answer from your profile, upload the CV.
  4. Take a full-page screenshot and save everything to the database.
  5. Never press Submit. That happens only from the review page, after you approve.
Jobs without a supported form get MANUAL_APPLY: the CV is ready and the review
page gives you the link.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from .. import database as db
from ..cv_tailor.single_template_tailor import build_cv, load_profile
from ..intelligence.answer_engine import AnswerEngine
from . import email_apply, form_fields

log = logging.getLogger("jobsprint")

ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com")


def application_url(job: dict) -> str | None:
    """Best guess at the page that holds the application form, or None if unsupported."""
    src = job.get("source") or ""
    url = job.get("apply_url") or job.get("url") or ""
    if src.startswith("lever:"):
        return url if url.rstrip("/").endswith("/apply") else url.rstrip("/") + "/apply"
    if src.startswith("ashby:"):
        base = (job.get("url") or url).rstrip("/")
        return base if base.endswith("/application") else base + "/application"
    if src.startswith("greenhouse:"):
        return job.get("url")
    host = urlparse(url).netloc
    if any(h in host for h in ATS_HOSTS):
        return url
    return None


APPLY_TEXT = re.compile(r"\bapply\b|application", re.I)
SKIP_TEXT = re.compile(r"auto-?apply|copilot|with ai|alert|newsletter|subscribe|similar jobs", re.I)
BLOCKED_TITLE = re.compile(r"just a moment|attention required|access denied|verify you are human", re.I)
ACCOUNT_URL = re.compile(r"/(sign-?up|sign-?in|log-?in|register|create-account|join|auth|account)(/|\?|$)", re.I)


def _account_page(page) -> bool:
    """Sign-up / login pages are NOT applications, even if they ask for name and email."""
    try:
        if ACCOUNT_URL.search(urlparse(page.url).path + "?"):
            return True
        # Only a password box you can see counts: many job boards hide a login pop-up on every page.
        return page.locator("input[type=password]:visible").count() > 0
    except Exception:
        return True


def _has_form(page) -> bool:
    """True when the page shows a real application form (not a search, newsletter or sign-up box)."""
    if _account_page(page):
        return False
    try:
        inputs = page.locator("input:visible, textarea:visible, select:visible").count()
        key = page.locator("input[type=email]:visible, input[type=file], input[name*=email i]:visible, input[id*=email i]:visible").count()
        return inputs >= 3 and key > 0
    except Exception:
        return False


def _blocked(page) -> bool:
    try:
        return bool(BLOCKED_TITLE.search(page.title() or ""))
    except Exception:
        return False


def _ats_url(href: str) -> str:
    fake = {"source": "", "apply_url": href, "url": href}
    if "lever.co" in href:
        fake["source"] = "lever:"
    elif "ashbyhq.com" in href:
        fake["source"] = "ashby:"
        fake["url"] = re.sub(r"/application/?$", "", href)
    return application_url(fake) or href


def _candidates(page):
    """Apply links/buttons in order of promise: ATS links, other-site links, same-site links, buttons."""
    here = urlparse(page.url)
    ats, external, internal, buttons = [], [], [], []
    for el in page.locator("a, button").all()[:600]:
        try:
            if not el.is_visible():
                continue
            text = ((el.inner_text(timeout=500) or "") + " " + (el.get_attribute("aria-label") or "")).strip()
            href = el.get_attribute("href") or ""
        except Exception:
            continue
        if SKIP_TEXT.search(text):
            continue
        if href and not href.startswith(("mailto:", "javascript:")):
            full = page.evaluate("h => new URL(h, location.href).href", href)
            u = urlparse(full)
            if any(h in u.netloc for h in ATS_HOSTS):
                ats.append(("link", full))
            elif not APPLY_TEXT.search(text):
                continue
            elif u.netloc == here.netloc and u.path == here.path:
                continue  # "#apply" jumps within the same page
            elif u.netloc != here.netloc:
                external.append(("link", full))
            else:
                internal.append(("link", full))
        elif APPLY_TEXT.search(text):
            buttons.append(("click", el))
    seen, out = set(), []
    for kind, v in ats + external + internal + buttons:
        key = v if kind == "link" else id(v)
        if key not in seen:
            seen.add(key)
            out.append((kind, v))
    return out[:5]


def _settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def _follow_to_ats(page, job: dict, depth: int = 2) -> str | None:
    """Follow a job-board listing through its Apply link to the company's real form."""
    try:
        page.goto(job.get("apply_url") or job["url"], wait_until="domcontentloaded")
        _settle(page)
    except Exception:
        return None
    for _ in range(depth + 1):
        if _blocked(page):
            return None
        if any(h in urlparse(page.url).netloc for h in ATS_HOSTS):
            return _ats_url(page.url)
        if _has_form(page):
            return page.url
        moved = False
        for kind, v in _candidates(page):
            before = page.url
            try:
                if kind == "link":
                    page.goto(v, wait_until="domcontentloaded")
                else:
                    try:
                        with page.context.expect_page(timeout=5000) as new:
                            v.click()
                        popup = new.value
                        popup.wait_for_load_state("domcontentloaded")
                        page.goto(popup.url, wait_until="domcontentloaded")
                        popup.close()
                    except Exception:
                        if page.url == before:
                            v.click()
                _settle(page)
            except Exception:
                continue
            if page.url != before:
                moved = True
                break
        if not moved:
            return None
    return page.url if _has_form(page) else None


def _google_then_retry(page, job: dict) -> str | None:
    """On a sign-in page: try "Continue with Google", then look for the form again."""
    import os

    from ..settings import load_settings
    from .google_signin import try_google
    cfg = load_settings()["google_sign_in"]
    account = os.environ.get("EMAIL_ADDRESS", "")
    if not (cfg["enabled"] and account):
        return None
    result = try_google(page, account, bool(cfg["allow_new_accounts"]))
    if result != "ok":
        if result == "needs_password":
            log.info("Google wants you to sign in first at %s. Run START_SPRINT.bat --login-setup once.", urlparse(page.url).netloc)
        return None
    if _has_form(page):
        return page.url
    return _follow_to_ats(page, job)


def _needs_login(conn, jid: int, cv_id: int, at_url: str) -> str:
    """The site wants an account. Run --login-setup, sign in once, and the next run retries it."""
    host = urlparse(at_url).netloc
    conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at, error) VALUES (?,?,?,?,?,?)",
                 (jid, cv_id, at_url, "NEEDS_LOGIN", db.now_iso(),
                  f"{host} wants you signed in. Run START_SPRINT.bat --login-setup once and sign in there "
                  "(or create your account); the next run fills this form. Or apply yourself with your tailored CV."))
    db.set_job_status(conn, jid, "NEEDS_LOGIN", f"sign in once at {host}")
    return "NEEDS_LOGIN"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:40]


def stage_job(page, conn, ws, job: dict, engine: AnswerEngine, profile: dict) -> str:
    jid = job["id"]
    cv_path, cv = build_cv(f"{job['title']}\n{job.get('description') or ''}",
                           ws.tailored_cvs / f"{jid}_{_safe(job.get('company'))}_{_safe(job['title'])}.docx",
                           profile=profile)
    cv_id = db.add_tailored_cv(conn, jid, cv.track, str(cv_path), cv.matched_keywords)

    try:  # start clean, so a sign-in wall left open by the previous job is never blamed on this one
        page.goto("about:blank")
    except Exception:
        pass
    direct = application_url(job)
    if not direct:  # "send your CV to hr@..." in the job post
        found = email_apply.find_apply_email(job.get("description") or "")
        if found:
            return email_apply.stage(conn, ws, job, cv_id, cv_path, cv.track, found, profile)
    url = direct or _follow_to_ats(page, job)
    if not url:
        try:
            found = email_apply.find_apply_email(page.inner_text("body", timeout=3000))
        except Exception:
            found = None
        if found:
            return email_apply.stage(conn, ws, job, cv_id, cv_path, cv.track, found, profile)
    if not url and _account_page(page) and not page.url.startswith("about:"):
        url = _google_then_retry(page, job)
        if not url:
            return _needs_login(conn, jid, cv_id, page.url)
    if not url:
        conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at, error) VALUES (?,?,?,?,?,?)",
                     (jid, cv_id, job.get("apply_url") or job["url"], "MANUAL_APPLY", db.now_iso(),
                      "No supported application form found. Your tailored CV is ready; apply from the link."))
        db.set_job_status(conn, jid, "MANUAL_APPLY", "apply yourself with the tailored CV")
        return "MANUAL_APPLY"

    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if _account_page(page):
        retry = _google_then_retry(page, job)
        if retry and retry != url:
            page.goto(retry, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        if _account_page(page):
            return _needs_login(conn, jid, cv_id, page.url)
        url = page.url
    fields = form_fields.discover(page)
    if not fields:
        conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at, error) VALUES (?,?,?,?,?,?)",
                     (jid, cv_id, url, "MANUAL_APPLY", db.now_iso(), "Could not find form fields on the page."))
        db.set_job_status(conn, jid, "MANUAL_APPLY", "form not readable; apply yourself")
        return "MANUAL_APPLY"

    answers, missing = [], []
    for f in fields:
        a = engine.answer(form_fields.to_question(f), job, str(cv_path))
        filled = form_fields.fill_field(page, f, a.value) if not a.consent else False
        rec = {"label": f["label"], "kind": f["kind"], "options": f.get("options", []), "required": f["required"],
               "value": a.value if not isinstance(a.value, bool) else ("Yes" if a.value else "No"),
               "source": a.source, "filled": filled, "needs_user": a.needs_user or (f["required"] and not filled and not a.consent),
               "consent": a.consent, "note": a.note}
        if f["kind"] == "file" and a.source == "cv":
            rec["value"] = cv_path.name
        answers.append(rec)
        if rec["needs_user"] and not a.consent:
            missing.append(f["label"])

    shot = ws.screenshots / f"{jid}_{_safe(job.get('company'))}_staged.png"
    page.screenshot(path=str(shot), full_page=True)
    state = "NEEDS_USER_INPUT" if missing else "READY_TO_SUBMIT"
    note = "CAPTCHA on this form: you'll solve it when you press Submit." if form_fields.has_captcha(page) else None
    cur = conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, answers_json, missing_json, state, error, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (jid, cv_id, url, json.dumps(answers), json.dumps(missing), state, note, db.now_iso()))
    conn.execute("INSERT INTO screenshots(staged_form_id, path, created_at) VALUES (?,?,?)", (cur.lastrowid, str(shot), db.now_iso()))
    db.set_job_status(conn, jid, state, f"missing: {', '.join(missing)}" if missing else None)
    return state


def run(ws, limit: int | None = None) -> dict:
    """Stage the top qualified jobs that haven't been staged yet."""
    from .browser_manager import browser
    profile = load_profile()
    limit = limit or int(profile.get("workspace", {}).get("daily_stage_target", 20))
    engine = AnswerEngine(profile)
    if not engine.use_llm:
        log.warning("No LLM key found in .env, so written questions will be left for you.")
    counts = {}
    with db.session(ws.db_path) as conn:
        pool = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE status = 'QUALIFIED' ORDER BY priority_score DESC LIMIT ?", (limit * 4,))]
    # Jobs that point straight at a Greenhouse, Lever or Ashby form go first: those fill reliably.
    def order(j):
        if application_url(j):
            return 0
        return 1 if email_apply.find_apply_email(j.get("description") or "") else 2
    jobs = sorted(pool, key=order)[:limit]
    if not jobs:
        log.info("No qualified jobs waiting. Run --discover first.")
        return counts
    log.info("Staging %d jobs. The browser fills each form and stops before Submit.", len(jobs))
    with browser(ws) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, job in enumerate(jobs, 1):
            with db.session(ws.db_path) as conn:
                try:
                    state = stage_job(page, conn, ws, job, engine, profile)
                except Exception as exc:  # one bad form must not stop the sprint
                    log.warning("Staging failed for %s at %s: %s", job["title"], job.get("company"), exc)
                    db.set_job_status(conn, job["id"], "FAILED", str(exc)[:200])
                    state = "FAILED"
            counts[state] = counts.get(state, 0) + 1
            log.info("[%d/%d] %s: %s @ %s", i, len(jobs), state, job["title"][:60], job.get("company"))
    log.info("Staging done: %s", ", ".join(f"{k} {v}" for k, v in counts.items()))
    return counts
