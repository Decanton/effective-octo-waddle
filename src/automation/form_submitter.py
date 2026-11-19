"""Submits a staged application: when you press Submit on the review page, or
automatically within the limits in config/agent_settings.yaml (see auto_submitter.py).

It reopens the form, fills it again from the saved answers plus anything you
typed on the review page, ticks only the consent boxes you ticked, presses
Submit, and waits (up to 3 minutes, so you can solve a CAPTCHA) for a
confirmation.
"""
from __future__ import annotations

import json
import logging
import os
import re

from .. import database as db
from . import form_fields

log = logging.getLogger("jobsprint")
CONFIRM = re.compile(r"thank you|thanks for applying|application (has been )?(received|submitted)|we.?ve received|successfully submitted", re.I)


def _key(label: str, kind: str) -> str:
    return f"{kind}|{' '.join(label.lower().split())}"


def submit(ws, staged_id: int, overrides: dict | None = None, consents: list[str] | None = None,
           confirm_wait_ms: int = 180_000) -> dict:
    from .browser_manager import browser
    with db.session(ws.db_path) as conn:
        r = conn.execute("SELECT form_url FROM staged_forms WHERE id = ?", (staged_id,)).fetchone()
    if r and (r["form_url"] or "").startswith("mailto:"):
        from .email_apply import send
        return send(ws, staged_id, overrides)
    overrides = {" ".join(k.lower().split()): v for k, v in (overrides or {}).items() if v not in (None, "")}
    consents = {" ".join(c.lower().split()) for c in (consents or [])}
    with db.session(ws.db_path) as conn:
        row = conn.execute("SELECT s.*, j.title, j.company FROM staged_forms s JOIN jobs j ON j.id = s.job_id WHERE s.id = ?",
                           (staged_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "staged form not found"}
        if row["state"] not in ("READY_TO_SUBMIT", "NEEDS_USER_INPUT"):
            return {"ok": False, "error": f"cannot submit a form in state {row['state']}"}
        saved = {_key(a["label"], a["kind"]): a for a in json.loads(row["answers_json"] or "[]")}
        cv = conn.execute("SELECT docx_path FROM tailored_cvs WHERE id = ?", (row["cv_id"],)).fetchone()

    # Visible by default so you can solve a CAPTCHA; JOBSPRINT_HEADLESS=1 is for tests.
    with browser(ws, headless=os.environ.get("JOBSPRINT_HEADLESS") == "1") as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(row["form_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if page.locator("input[type=password]:visible").count() > 0:
            return {"ok": False, "error": "this page asks for a password, so it is a sign-up or login page, not an application. Nothing was submitted."}
        missing = []
        for f in form_fields.discover(page):
            label_n = " ".join(f["label"].lower().split())
            a = saved.get(_key(f["label"], f["kind"]), {})
            if f["kind"] == "file" and a.get("source") == "cv" and cv:
                value = cv["docx_path"]
            elif label_n in overrides:
                value = overrides[label_n]
            elif a.get("consent"):
                value = True if label_n in consents else None
                if value and f["kind"] in ("radio", "checkboxes"):
                    value = next((o for o in f["options"] if re.search(r"\b(yes|agree|accept|i do|confirm)", o, re.I)), None)
            else:
                value = a.get("value")
            ok = form_fields.fill_field(page, f, value)
            if f["required"] and not ok:
                missing.append(f["label"])
        if missing:
            shot = ws.screenshots / f"{row['job_id']}_not_submitted.png"
            page.screenshot(path=str(shot), full_page=True)
            return {"ok": False, "error": "still missing: " + ", ".join(missing), "screenshot": str(shot)}
        btn = form_fields.find_submit(page)
        if btn is None:
            return {"ok": False, "error": "could not find the Submit button; submit it yourself in the open browser"}
        log.info("Submitting %s at %s", row["title"], row["company"])
        btn.click()
        try:
            page.wait_for_function("re => new RegExp(re, 'i').test(document.body.innerText)",
                                   arg=CONFIRM.pattern, timeout=confirm_wait_ms)
            confirmed = True
        except Exception:
            confirmed = bool(CONFIRM.search(page.inner_text("body") or ""))
        shot = ws.screenshots / f"{row['job_id']}_submitted.png"
        page.screenshot(path=str(shot), full_page=True)

    with db.session(ws.db_path) as conn:
        if confirmed:
            conn.execute("UPDATE staged_forms SET state = 'SUBMITTED', submitted_at = ? WHERE id = ?", (db.now_iso(), staged_id))
            db.set_job_status(conn, row["job_id"], "SUBMITTED")
        else:
            # It was very likely sent, so never send it again; flag it for you to check.
            conn.execute("UPDATE staged_forms SET state = 'SENT_UNCONFIRMED', submitted_at = ?, error = ? WHERE id = ?",
                         (db.now_iso(), "Pressed Submit but saw no thank-you page. Check the screenshot.", staged_id))
            db.set_job_status(conn, row["job_id"], "SUBMITTED", "pressed Submit; no confirmation page seen")
        conn.execute("INSERT INTO screenshots(staged_form_id, path, kind, created_at) VALUES (?,?,?,?)",
                     (staged_id, str(shot), "after_submit", db.now_iso()))
    return {"ok": confirmed, "screenshot": str(shot),
            "error": None if confirmed else "no confirmation seen; check the screenshot"}
