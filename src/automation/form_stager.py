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
from . import form_fields

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


def _follow_to_ats(page, job: dict) -> str | None:
    """Open a job-board page and look for a link to a supported application form."""
    try:
        page.goto(job.get("apply_url") or job["url"], wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    except Exception:
        return None
    if any(h in urlparse(page.url).netloc for h in ATS_HOSTS):
        return page.url
    for a in page.locator("a[href]").all()[:400]:
        href = a.get_attribute("href") or ""
        if any(h in href for h in ATS_HOSTS):
            fake = {"source": "", "apply_url": href, "url": href}
            if "lever.co" in href:
                fake["source"] = "lever:"
            elif "ashbyhq.com" in href:
                fake["source"] = "ashby:"
                fake["url"] = re.sub(r"/application/?$", "", href)
            return application_url(fake) or href
    return None


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:40]


def stage_job(page, conn, ws, job: dict, engine: AnswerEngine, profile: dict) -> str:
    jid = job["id"]
    cv_path, cv = build_cv(f"{job['title']}\n{job.get('description') or ''}",
                           ws.tailored_cvs / f"{jid}_{_safe(job.get('company'))}_{_safe(job['title'])}.docx",
                           profile=profile)
    cv_id = db.add_tailored_cv(conn, jid, cv.track, str(cv_path), cv.matched_keywords)

    url = application_url(job) or _follow_to_ats(page, job)
    if not url:
        conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at, error) VALUES (?,?,?,?,?,?)",
                     (jid, cv_id, job.get("apply_url") or job["url"], "MANUAL_APPLY", db.now_iso(),
                      "No supported application form found. Your tailored CV is ready; apply from the link."))
        db.set_job_status(conn, jid, "MANUAL_APPLY", "apply yourself with the tailored CV")
        return "MANUAL_APPLY"

    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if re.search(r"/(login|signin|sign-in)\b", page.url):
        db.set_job_status(conn, jid, "NEEDS_LOGIN", "the site asked you to sign in; run --login-setup")
        return "NEEDS_LOGIN"
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
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE status = 'QUALIFIED' ORDER BY priority_score DESC LIMIT ?", (limit,))]
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
