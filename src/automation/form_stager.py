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
from . import human_assist_interceptor as hia

log = logging.getLogger("jobsprint")

# Application systems whose pages are real application forms (company career sites run on these).
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "jobvite.com",
             "bamboohr.com", "recruitee.com", "teamtailor.com", "personio.de", "personio.com", "breezy.hr",
             "applytojob.com", "icims.com", "myworkdayjobs.com", "rippling.com", "jazzhr.com", "pinpointhq.com",
             "freshteam.com", "zohorecruit.com", "manatal.com", "dover.com", "gem.com", "comeet.com")


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
CLOSED_TEXT = re.compile(r"no longer (accepting|available|open)|position (has been|is) (filled|closed)|job (is|has) (closed|expired)|"
                         r"this (job|posting|position|role) (is|has) (closed|expired|been (removed|filled))|"
                         r"(job|page) (not found|doesn.?t exist)|applications? (are|is) (now )?closed|vacancy (is )?closed", re.I)


def _closed(page) -> bool:
    try:
        return bool(CLOSED_TEXT.search((page.inner_text("body", timeout=3000) or "")[:6000]))
    except Exception:
        return False


BLOCKED_TITLE = re.compile(r"just a moment|attention required|access denied|verify you are human", re.I)
ACCOUNT_URL = re.compile(r"/(sign-?up|sign-?in|log-?in|register|create-account|join|auth|account|accounts|onboarding|"
                         r"welcome|get-started|getting-started|profile/(setup|create|edit)|settings|subscribe|pricing|"
                         r"checkout|upgrade|premium)(/|\?|$)|/step_?\d", re.I)


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
        email = page.locator("input[type=email]:visible, input[name*=email i]:visible, input[id*=email i]:visible").count()
        # A real application asks for your CV. Off Greenhouse/Lever/Ashby, a page without a
        # file upload is a search box, newsletter, sign-up or onboarding step, not an application.
        upload = page.locator("input[type=file]").count()
        on_ats = any(h in urlparse(page.url).netloc for h in ATS_HOSTS)
        return inputs >= 3 and (upload > 0 if not on_ats else (upload > 0 or email > 0))
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


def _follow(page, job: dict, depth: int = 3):
    """Follow a job-board listing through its Apply link to the company's real form.

    Returns (url, active_page). When Apply opens the company site in a new tab, the
    agent keeps working in THAT tab, so the site's session (and sign-in) stays intact.
    """
    try:
        page.goto(job.get("apply_url") or job["url"], wait_until="domcontentloaded")
        _settle(page)
    except Exception:
        return None, page
    for _ in range(depth + 1):
        if _blocked(page):
            return None, page
        if any(h in urlparse(page.url).netloc for h in ATS_HOSTS) and not _account_page(page):
            if _has_form(page):
                return page.url, page
            nxt = _ats_url(page.url)
            if nxt != page.url:
                try:
                    page.goto(nxt, wait_until="domcontentloaded")
                    _settle(page)
                except Exception:
                    pass
            if _has_form(page):
                return page.url, page
        elif _has_form(page):
            return page.url, page
        moved = False
        for kind, v in _candidates(page):
            before = page.url
            try:
                if kind == "link":
                    page.goto(v, wait_until="domcontentloaded")
                else:
                    try:
                        with page.context.expect_page(timeout=6000) as new:
                            v.click()
                        popup = new.value  # the company's site opened in a new tab: switch to it
                        popup.wait_for_load_state("domcontentloaded")
                        page = popup
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
            return None, page
    return (page.url if _has_form(page) else None), page


def _follow_to_ats(page, job: dict, depth: int = 3) -> str | None:
    return _follow(page, job, depth)[0]


def _sign_in_then_retry(page, job: dict, ws=None) -> str | None:
    """On a sign-in page: try "Continue with Google", then a portal account (sign in, or create one
    and confirm the email code), then look for the application form again."""
    import os

    from ..settings import load_settings
    from .google_signin import try_google
    settings = load_settings()
    cfg = settings["google_sign_in"]
    account = os.environ.get("EMAIL_ADDRESS", "")
    if not account:
        return None
    result = "off"
    if cfg["enabled"]:
        result = try_google(page, account, bool(cfg["allow_new_accounts"]))
        if result == "needs_password":
            log.info("Google wants you to sign in first at %s. Open Job Agent Chrome and sign in once.", urlparse(page.url).netloc)
    if result != "ok" and ws is not None and settings["portal_accounts"]["enabled"] and _account_page(page):
        from .portal_accounts import sign_in_or_register
        result = sign_in_or_register(page, ws, account, settings["portal_accounts"])
    if result != "ok":
        return None
    _settle(page)
    if _has_form(page):
        return page.url
    url, active = _follow(page, job)
    if url and active is not page:
        page.goto(url, wait_until="domcontentloaded")
    return url


_google_then_retry = _sign_in_then_retry  # older name


def _needs_login(conn, jid: int, cv_id: int, at_url: str) -> str:
    """The site wants an account. Run --login-setup, sign in once, and the next run retries it."""
    host = urlparse(at_url).netloc
    conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at, error) VALUES (?,?,?,?,?,?)",
                 (jid, cv_id, at_url, "NEEDS_LOGIN", db.now_iso(),
                  f"{host} wants you signed in. Run START_SPRINT.bat --login-setup once and sign in there "
                  "(or create your account); the next run fills this form. Or apply yourself with your tailored CV."))
    db.set_job_status(conn, jid, "NEEDS_LOGIN", f"sign in once at {host}")
    return "NEEDS_LOGIN"


def _needs_manual(conn, jid: int, cv_id: int, at_url: str, kind: str) -> str:
    what = "a CAPTCHA" if kind == "captcha" else "a code texted to your phone"
    conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at, error) VALUES (?,?,?,?,?,?)",
                 (jid, cv_id, at_url, "NEEDS_MANUAL_OTP", db.now_iso(),
                  f"{urlparse(at_url).netloc} asked for {what} and nobody answered in time. The next run tries again "
                  "(keep Google Messages open in Job Agent Chrome for SMS codes), or apply yourself with the tailored CV."))
    db.set_job_status(conn, jid, "NEEDS_MANUAL_OTP", f"{what} not answered")
    return "NEEDS_MANUAL_OTP"


def _assist(page, job: dict) -> str:
    """Hand a blocking CAPTCHA or SMS-code prompt to you (or read the SMS), then continue. Returns the kind left unsolved, or ''."""
    kind = hia.detect(page)
    if kind is None or (kind == "captcha" and not hia.blocking_challenge(page)):
        return ""  # a CAPTCHA inside the form itself is solved when it's submitted
    return "" if hia.assist(page, job, kind=kind) != "timeout" else kind


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
    active = page
    if direct:
        url = direct
    else:
        url, active = _follow(page, job)
    if not url:
        try:
            found = email_apply.find_apply_email(active.inner_text("body", timeout=3000))
        except Exception:
            found = None
        if found:
            return email_apply.stage(conn, ws, job, cv_id, cv_path, cv.track, found, profile)
    if not url and _account_page(active) and not active.url.startswith("about:"):
        url = _sign_in_then_retry(active, job, ws)
        if not url:
            return _needs_login(conn, jid, cv_id, active.url)
    if not url:
        conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at, error) VALUES (?,?,?,?,?,?)",
                     (jid, cv_id, job.get("apply_url") or job["url"], "MANUAL_APPLY", db.now_iso(),
                      "No supported application form found. Your tailored CV is ready; apply from the link."))
        db.set_job_status(conn, jid, "MANUAL_APPLY", "apply yourself with the tailored CV")
        return "MANUAL_APPLY"

    page = active  # keep working in the tab the company's site opened in
    if page.url != url:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
    left = _assist(page, job)
    if left:
        return _needs_manual(conn, jid, cv_id, page.url, left)
    if _closed(page) and not form_fields.discover(page):
        conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at, error) VALUES (?,?,?,?,?,?)",
                     (jid, cv_id, url, "SKIPPED", db.now_iso(), "The job is closed or the posting was removed."))
        db.set_job_status(conn, jid, "SKIPPED", "job closed")
        return "SKIPPED"
    if _account_page(page):
        retry = _sign_in_then_retry(page, job, ws)
        if retry and retry != url:
            page.goto(retry, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        left = _assist(page, job)
        if left:
            return _needs_manual(conn, jid, cv_id, page.url, left)
        if _account_page(page):
            return _needs_login(conn, jid, cv_id, page.url)
        url = page.url
        hia.pause()
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
        if filled:
            hia.pause("field")
        rec = {"label": f["label"], "kind": f["kind"], "options": f.get("options", []), "required": f["required"],
               "value": a.value if not isinstance(a.value, bool) else ("Yes" if a.value else "No"),
               "source": a.source, "filled": filled, "needs_user": a.needs_user or (f["required"] and not filled and not a.consent),
               "consent": a.consent, "note": a.note}
        if f["kind"] == "file" and a.source == "cv":
            rec["value"] = cv_path.name
        answers.append(rec)
        if rec["needs_user"] and not a.consent:
            missing.append(f["label"])

    left = _assist(page, job)  # e.g. a phone check that appeared while filling
    if left:
        return _needs_manual(conn, jid, cv_id, page.url, left)
    # Strict check, as a real Submit would see it: every required field filled, CV attached, no errors shown.
    consent_ids = tuple(f["id"] for f, a in zip(fields, answers) if a["consent"])
    problems = form_fields.verify(page, consent_ids)
    if problems:
        log.info("  not verified: %s", "; ".join(problems[:5]))
        missing += [p for p in problems if p not in missing]
    shot = ws.screenshots / f"{jid}_{_safe(job.get('company'))}_staged.png"
    page.screenshot(path=str(shot), full_page=True)
    state = "NEEDS_USER_INPUT" if missing else "READY_TO_SUBMIT"
    note = None
    if hia.detect(page) == "captcha":  # a visible CAPTCHA on the form: can't finish unattended; kept on the optional list
        state, note = "NEEDS_MANUAL_OTP", "This form has a CAPTCHA, so it can't be sent unattended. Everything else is filled; press Submit and tick the CAPTCHA when you're around."
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
            "SELECT * FROM jobs WHERE status = 'QUALIFIED' ORDER BY priority_score DESC LIMIT 600")]
        walls = {urlparse(r[0]).netloc for r in conn.execute(
            "SELECT form_url FROM staged_forms WHERE state IN ('NEEDS_MANUAL_OTP','NEEDS_LOGIN') AND created_at >= date('now','-14 days')")}
    # What finishes with nobody there goes first: "email your CV" posts, then Greenhouse/Lever/Ashby/Workable
    # forms, then the rest; sites that showed a CAPTCHA or sign-in wall lately go last.
    def order(j):
        if email_apply.find_apply_email(j.get("description") or "") and not application_url(j):
            return 0
        if application_url(j):
            return 1
        return 3 if urlparse(j.get("apply_url") or j.get("url") or "").netloc in walls else 2
    jobs = sorted(pool, key=order)[:limit]
    if not jobs:
        log.info("No qualified jobs waiting. Run --discover first.")
        return counts
    log.info("Staging %d jobs. The browser fills each form and stops before Submit.", len(jobs))
    with browser(ws) as ctx:
        page = ctx.pages[-1] if ctx.pages else ctx.new_page()  # the agent's own tab
        yours = {p for p in ctx.pages if p is not page}  # tabs you had open: never touched
        for i, job in enumerate(jobs, 1):
            if page.is_closed():  # a site closed our tab: open a fresh one
                page = ctx.new_page()
            for extra in list(ctx.pages):  # tabs the previous job opened
                if extra is not page and extra not in yours and not extra.is_closed():
                    try:
                        extra.close()
                    except Exception:
                        pass
            with db.session(ws.db_path) as conn:
                try:
                    state = stage_job(page, conn, ws, job, engine, profile)
                except Exception as exc:  # one bad form must not stop the sprint
                    log.warning("Staging failed for %s at %s: %s", job["title"], job.get("company"), exc)
                    db.set_job_status(conn, job["id"], "FAILED", str(exc)[:200])
                    state = "FAILED"
                    if "closed" in str(exc).lower():
                        try:
                            page = ctx.new_page()
                        except Exception:
                            log.error("The browser closed. Stopping this staging run; the rest wait for the next run.")
                            break
            counts[state] = counts.get(state, 0) + 1
            log.info("[%d/%d] %s: %s @ %s", i, len(jobs), state, job["title"][:60], job.get("company"))
    log.info("Staging done: %s", ", ".join(f"{k} {v}" for k, v in counts.items()))
    return counts
