"""One polite follow-up for email applications that got no answer after a week.

Only applications that were emailed to a person's address (never a form, never a mailer),
only once per application, and only when no reply from that company has arrived.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from .. import analytics
from .. import database as db

log = logging.getLogger("jobsprint")


def body(title: str, company: str | None, name: str) -> str:
    return (f"Dear Hiring Manager,\n\nI'm following up on my application for the {title} role"
            f"{' at ' + company if company else ''}, sent last week with my CV and cover letter. "
            "I remain very interested and would be glad to share anything else you need.\n\n"
            f"Thank you for your time.\n\nKind regards,\n{name}")


def run(conn, cfg: dict, profile: dict, smtp_factory=None) -> dict:
    out = {"sent": 0, "due": 0}
    if not cfg.get("enabled", True):
        return out
    res = analytics.results(conn, days=45, follow_after=int(cfg.get("after_days", 7)))
    due = [a for a in res["applications"] if a["follow_up_due"] and a["method"] == "email"]
    out["due"] = len(due)
    if not cfg.get("auto_send", True) or not due:
        return out
    from .recruiter_replies import machine_sender
    addr, pw = os.environ.get("EMAIL_ADDRESS"), os.environ.get("EMAIL_APP_PASSWORD")
    if not (addr and pw):
        return out
    name = profile["personal"]["full_name"]
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(host, port, timeout=60))
    for a in due[: int(cfg.get("max_per_day", 10))]:
        row = conn.execute("SELECT form_url, receipt_json FROM staged_forms WHERE id = ?", (a["id"],)).fetchone()
        to = (row["form_url"] or "")[7:]
        if not to or machine_sender(to):
            continue
        receipt = json.loads(row["receipt_json"] or "{}")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Message-ID"] = f"{name} <{addr}>", to, make_msgid()
        msg["Subject"] = "Re: " + (receipt.get("subject") or f"Application for {a['title']}")
        if receipt.get("message_id"):  # lands in the same thread as the application
            msg["In-Reply-To"] = msg["References"] = receipt["message_id"]
        msg.set_content(body(a["title"], a["company"], name))
        try:
            with factory() as s:
                s.login(addr, pw.replace(" ", ""))
                s.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("Follow-up to %s not sent: %s", to, exc)
            continue
        conn.execute("INSERT OR IGNORE INTO followups(staged_form_id, to_addr, sent_at) VALUES (?,?,?)", (a["id"], to, db.now_iso()))
        out["sent"] += 1
        log.info("Followed up on %s @ %s (%s)", a["title"], a["company"], to)
    return out
