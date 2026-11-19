"""LinkedIn Easy Apply in Job Agent Chrome (Kelly turned this on, up to 40 a day).

For a LinkedIn job the agent opens the post while signed in to LinkedIn:
  * "Easy Apply": it fills each step of LinkedIn's form with your answers, uploads the tailored
    CV, unticks "Follow company", and presses Submit when every step is complete. If LinkedIn
    asks something it can't answer, it discards the draft and the job waits for you.
  * "Apply" (on the company's site): it follows the button to the employer's form, which the
    normal filler then handles.
Pauses between steps are human-paced, and there is a daily cap (linkedin_easy_apply.max_per_day).
It never solves a CAPTCHA or LinkedIn security check: the job is skipped instead.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from .. import database as db
from . import form_fields
from . import human_assist_interceptor as hia

log = logging.getLogger("jobsprint")

MODAL = ".jobs-easy-apply-modal, [data-test-modal-id='easy-apply-modal'], div[role='dialog']"
APPLY_BTN = "button.jobs-apply-button, button[aria-label*='Easy Apply' i], button[aria-label*='Apply to' i], button[aria-label*='Apply on company' i]"
SENT = re.compile(r"application (was )?sent|your application was sent|application submitted|you applied|applied \d+ (seconds?|minutes?) ago", re.I)
CHECKPOINT = re.compile(r"/checkpoint/|/authwall|security verification|let'?s do a quick security check", re.I)
def inside(sel: str) -> str:
    """`sel` inside any of the Easy Apply modal selectors."""
    return ", ".join(f"{m.strip()} {sel}" for m in MODAL.split(","))


ERR = ".artdeco-inline-feedback--error, [data-test-form-element-error-messages], .fb-dash-form-element-error"


def is_linkedin(url: str | None) -> bool:
    return "linkedin.com" in urlparse(url or "").netloc


def job_url(url: str) -> str:
    m = re.search(r"(?:currentJobId=|/jobs/view/(?:[^/]*-)?)(\d{6,})", url or "")
    return f"https://www.linkedin.com/jobs/view/{m.group(1)}/" if m else url


_google_tried = False


def ensure_signed_in(page) -> bool:
    """Signed in to LinkedIn in Job Agent Chrome, trying "Continue with Google" once per run if not."""
    global _google_tried
    if signed_in(page):
        return True
    if _google_tried or os.environ.get("JOBSPRINT_HEADLESS") == "1":  # a hidden browser never signs in anywhere
        return False
    _google_tried = True
    account = os.environ.get("EMAIL_ADDRESS", "")
    if not account:
        return False
    from .google_signin import try_google
    try:
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        try_google(page, account, False)
        page.wait_for_timeout(4000)
    except Exception as exc:
        log.info("LinkedIn sign-in with Google didn't work: %s", exc)
    return signed_in(page)


def signed_in(page) -> bool:
    try:
        return any(c["name"] == "li_at" and "linkedin.com" in c["domain"] for c in page.context.cookies("https://www.linkedin.com"))
    except Exception:
        return False


def sent_today(conn) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return conn.execute("SELECT COUNT(*) FROM staged_forms WHERE state = 'SUBMITTED' AND form_url LIKE '%linkedin.com%' "
                        "AND substr(submitted_at, 1, 10) = ?", (today,)).fetchone()[0]


def _pause(lo=1.5, hi=4.0):
    if not os.environ.get("JOBSPRINT_FAST"):
        time.sleep(random.uniform(lo, hi) if os.environ.get("JOBSPRINT_HEADLESS") != "1" else 0.2)


def _visible(loc):
    try:
        return [el for el in loc.all() if el.is_visible()]
    except Exception:
        return []


def _button(page, rx: str):
    for el in _visible(page.locator(inside("button"))):
        try:
            text = " ".join([(el.get_attribute("aria-label") or ""), el.inner_text() or ""])
        except Exception:
            continue
        if re.search(rx, text, re.I):
            return el
    return None


def open_job(page, job: dict) -> str:
    """'easy', 'external', 'applied', 'checkpoint' or 'none'. Leaves the page on the job post."""
    page.goto(job_url(job.get("url") or ""), wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if CHECKPOINT.search(page.url) or CHECKPOINT.search((page.inner_text("body") or "")[:2000]):
        return "checkpoint"
    body = page.inner_text("body") or ""
    if re.search(r"\bapplied\b.{0,30}\bago\b|application submitted|no longer accepting applications", body[:6000], re.I):
        return "applied" if "no longer" not in body[:6000].lower() else "closed"
    for el in _visible(page.locator(APPLY_BTN)):
        text = (el.get_attribute("aria-label") or "") + " " + (el.inner_text() or "")
        return "easy" if re.search(r"easy apply", text, re.I) else "external"
    return "none"


def external_target(page):
    """Click LinkedIn's Apply button and return the employer's page (often a new tab)."""
    btn = _visible(page.locator(APPLY_BTN))
    if not btn:
        return None
    try:
        with page.context.expect_page(timeout=10000) as info:
            btn[0].click()
        new = info.value
        new.wait_for_load_state("domcontentloaded")
        return new
    except Exception:
        page.wait_for_timeout(2000)
        return page if not is_linkedin(page.url) else None


def _fill_step(page, engine, job, cv_path, answers, missing) -> None:
    for f in form_fields.discover(page, MODAL):
        loc = page.locator(f'[data-jsa="{f["id"]}"]') if f["kind"] not in ("radio", "checkboxes") else None
        if f["kind"] == "file":
            ok = form_fields.fill_field(page, f, str(cv_path))
            answers.append({"label": f["label"] or "Resume", "kind": "file", "value": cv_path.name, "source": "cv", "filled": ok,
                            "required": f["required"], "options": [], "needs_user": False, "consent": False, "note": None})
            continue
        try:  # LinkedIn fills your email/phone itself: keep what's there
            if loc is not None and f["kind"] in form_fields.TEXT_KINDS and (loc.input_value() or "").strip():
                continue
            if loc is not None and f["kind"] == "select" and (loc.input_value() or "") not in ("", "Select an option"):
                continue
        except Exception:
            pass
        a = engine.answer(form_fields.to_question(f), job, str(cv_path))
        if re.search(r"follow .*company|stay up to date", f["label"], re.I):
            continue
        ok = form_fields.fill_field(page, f, a.value) if not a.consent else False
        answers.append({"label": f["label"], "kind": f["kind"], "options": f.get("options", []), "required": f["required"],
                        "value": a.value if not isinstance(a.value, bool) else ("Yes" if a.value else "No"),
                        "source": a.source, "filled": ok, "needs_user": bool(f["required"] and not ok), "consent": a.consent,
                        "note": a.note})
        if f["required"] and not ok:
            missing.append(f["label"])
        hia.pause("field")


def _untick_follow(page) -> None:
    for el in _visible(page.locator(inside("input[type=checkbox]"))):
        try:
            lab = el.evaluate("e => (document.querySelector(`label[for='${e.id}']`) || e.closest('label') || {}).innerText || ''")
            if re.search(r"follow", lab, re.I) and el.is_checked():
                el.uncheck(force=True)
        except Exception:
            continue


def _discard(page) -> None:
    try:
        d = _button(page, r"^dismiss|dismiss$")
        if d:
            d.click()
            page.wait_for_timeout(800)
        for el in _visible(page.locator("button")):
            if re.search(r"^\s*discard\s*$", el.inner_text() or "", re.I):
                el.click()
                break
    except Exception:
        pass


def apply(page, conn, ws, job: dict, cv_id: int, cv_path, engine, max_steps: int = 12) -> str:
    """Run Easy Apply for one job. Returns the staged state."""
    jid, url = job["id"], job_url(job.get("url") or "")
    btn = [el for el in _visible(page.locator(APPLY_BTN)) if re.search(r"easy apply", (el.get_attribute("aria-label") or "") + (el.inner_text() or ""), re.I)]
    if not btn:
        return "none"
    btn[0].click()
    page.wait_for_timeout(2000)
    answers, missing = [], []
    for _step in range(max_steps):
        if hia.detect(page) == "captcha":
            _discard(page)
            missing.append("LinkedIn showed a security check")
            break
        _fill_step(page, engine, job, cv_path, answers, missing)
        _untick_follow(page)
        submit = _button(page, r"submit application")
        if submit is not None:
            if missing:
                break
            problems = form_fields.verify(page, (), MODAL)
            if problems:
                missing += problems
                break
            submit.click()
            page.wait_for_timeout(3000)
            try:
                page.wait_for_function("re => new RegExp(re, 'i').test(document.body.innerText)", arg=SENT.pattern, timeout=15000)
            except Exception:
                pass
            body = page.inner_text("body") or ""
            m = SENT.search(body)
            receipt = {"method": "linkedin_easy_apply", "url": url, "submitted_at": db.now_iso(),
                       "confirmation": body[max(0, m.start() - 60): m.end() + 120].strip() if m else None}
            state = "SUBMITTED" if m else "SENT_UNCONFIRMED"
            conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, answers_json, missing_json, state, created_at, submitted_at, receipt_json, error) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", (jid, cv_id, url, json.dumps(answers), "[]", state, db.now_iso(), db.now_iso(),
                                                          json.dumps(receipt), None if m else "Pressed Submit on LinkedIn; no confirmation seen."))
            db.set_job_status(conn, jid, "SUBMITTED", "LinkedIn Easy Apply" + ("" if m else " (no confirmation seen)"))
            db.audit(conn, "linkedin_easy_apply" if m else "form_submitted_unconfirmed", url, f"{job['title']} @ {job.get('company')}")
            for el in _visible(page.locator("button[aria-label*='Dismiss' i]")):
                try:
                    el.click()
                except Exception:
                    pass
            log.info("  LinkedIn Easy Apply sent: %s @ %s", job["title"][:60], job.get("company"))
            return state
        nxt = _button(page, r"continue to next step|review your application|^\s*next\s*$|^\s*review\s*$")
        if nxt is None:
            missing.append("LinkedIn's form had no Next or Submit button")
            break
        before = page.locator(MODAL).first.inner_text() if page.locator(MODAL).count() else ""
        nxt.click()
        _pause()
        errs = _visible(page.locator(MODAL).first.locator(ERR)) if page.locator(MODAL).count() else []
        after = page.locator(MODAL).first.inner_text() if page.locator(MODAL).count() else ""
        if errs and after == before:
            missing += [e.inner_text().strip()[:120] for e in errs[:5]] or ["LinkedIn didn't accept a step"]
            break
    _discard(page)
    conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, answers_json, missing_json, state, created_at, error) VALUES (?,?,?,?,?,?,?,?)",
                 (jid, cv_id, url, json.dumps(answers), json.dumps(missing), "NEEDS_USER_INPUT", db.now_iso(),
                  "LinkedIn Easy Apply needs answers the agent doesn't have; the draft was discarded. Add them to your profile and the next run retries."))
    db.set_job_status(conn, jid, "NEEDS_USER_INPUT", "LinkedIn asked: " + ", ".join(missing[:4]))
    return "NEEDS_USER_INPUT"
