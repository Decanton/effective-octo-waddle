"""Drafts replies to recruiter emails; sends them when you press Send, or automatically
(recruiter_replies.auto_send in config/agent_settings.yaml) when the draft has no gaps.

Drafts are grounded in your profile (no invented facts). Sending uses SMTP with
EMAIL_ADDRESS / EMAIL_APP_PASSWORD from .env and replies in the same thread.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from .. import database as db
from .. import llm_client

log = logging.getLogger("jobsprint")

SYSTEM = ("You write short, warm, professional replies from a job seeker to a recruiter. Use ONLY facts from the "
          "candidate profile given. Never invent dates, times, salaries, availability or experience: where the recruiter "
          "asks for something the profile doesn't have, write [FILL IN: what is needed]. Never accept or decline an "
          "offer. No subject line. "
          "Sign with the candidate's full name.")


def _profile_brief(profile: dict) -> str:
    p = profile.get("personal", {})
    sa = {k: v for k, v in (profile.get("screening_answers") or {}).items() if v is not None and not k.startswith("_")}
    return (f"Name: {p.get('full_name')}\nEmail: {p.get('email')}\nPhone: {p.get('phone')}\nLocation: {p.get('location')}\n"
            f"LinkedIn: {p.get('linkedin')}\nPortfolio: {p.get('portfolio')}\n"
            f"Summary: {(profile.get('summaries') or {}).get('general', '')}\nKnown answers: {sa}")


def fallback_draft(m: dict, profile: dict) -> str:
    name = profile.get("personal", {}).get("full_name", "")
    first = (m.get("from_name") or "").split(" ")[0] or "there"
    cat = m.get("category")
    if cat == "rejection":
        body = "Thank you for letting me know, and for considering me. I'd be glad to be kept in mind for future roles."
    elif cat == "interview_request":
        body = ("Thank you for reaching out. I'd be happy to speak. I'm available [FILL IN: two or three times, "
                "with your time zone (EAT)]. Please let me know what works for you.")
    else:
        body = ("Thank you for reaching out about this opportunity. I'm interested and would be glad to learn more. "
                "I'd be happy to share my CV and answer any questions.")
    return f"Hi {first},\n\n{body}\n\nBest regards,\n{name}"


def draft(m: dict, profile: dict) -> str:
    if llm_client.available():
        prompt = (f"Candidate profile:\n{_profile_brief(profile)}\n\nRecruiter email from {m.get('from_name')} "
                  f"<{m.get('from_addr')}>, subject '{m.get('subject')}':\n{(m.get('text') or '')[:4000]}\n\nWrite the reply.")
        try:
            text = llm_client.complete(prompt, SYSTEM)
            from ..intelligence.answer_engine import _unverified_numbers
            bad = _unverified_numbers(text, _profile_brief(profile) + " " + (m.get("text") or "") + " " + (m.get("subject") or ""))
            if bad:  # a time, date or figure the AI made up: leave it for you
                text += f"\n\n[FILL IN: check these details before sending: {', '.join(bad)}]"
            return text
        except Exception as exc:
            log.info("AI draft failed (%s); using a plain template.", exc)
    return fallback_draft(m, profile)


def save(conn, messages: list[dict], profile: dict) -> int:
    added = 0
    for m in messages:
        if conn.execute("SELECT 1 FROM recruiter_emails WHERE message_id = ?", (m["message_id"],)).fetchone():
            continue
        conn.execute("INSERT INTO recruiter_emails(message_id, thread_id, from_addr, from_name, subject, received_at, "
                     "body_text, category, draft_reply, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (m["message_id"], m.get("thread_id"), m["from_addr"], m.get("from_name"), m.get("subject"),
                      m.get("received_at"), (m.get("text") or "")[:20000], m.get("category"),
                      draft(m, profile), "DRAFTED", db.now_iso()))
        added += 1
    return added


INTERVIEW_PLATFORM = re.compile(r"complete your (ai )?interview|moving forward.{0,40}complete|take (the|your) assessment|"
                                r"your (application|profile) (is|has been) (moving|reviewed)|start your (interview|assessment)", re.I)


def machine_sender(from_addr: str, subject: str = "") -> bool:
    """A job site's or platform's automatic mailer, not a person. Never answered."""
    from .imap_scanner import ALERT_SENDERS, AUTOMATED, PLATFORM_SUBJECT
    addr = (from_addr or "").lower()
    local, _, domain = addr.partition("@")
    return bool(AUTOMATED.search(addr) or AUTOMATED.search(local) or any(d in domain for d in ALERT_SENDERS)
                or PLATFORM_SUBJECT.search(subject or "") or INTERVIEW_PLATFORM.search(subject or ""))


def auto_send(conn, cfg: dict, smtp_factory=None) -> dict:
    """Send complete drafts, within the daily cap. Drafts with [FILL IN ...] gaps wait for you."""
    out = {"sent": 0, "waiting": 0}
    if not cfg.get("auto_send"):
        return out
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    budget = int(cfg.get("max_per_day", 10)) - conn.execute(
        "SELECT COUNT(*) FROM recruiter_emails WHERE state = 'SENT' AND substr(sent_at, 1, 10) = ?", (today,)).fetchone()[0]
    skip = set(cfg.get("skip_categories") or [])
    for row in conn.execute("SELECT * FROM recruiter_emails WHERE state = 'DRAFTED' ORDER BY received_at").fetchall():
        body = row["draft_reply"] or ""
        own = (os.environ.get("EMAIL_ADDRESS") or "").lower()
        replied = conn.execute("SELECT 1 FROM recruiter_emails WHERE state = 'SENT' AND id != ? AND (from_addr = ? "
                               "OR (thread_id IS NOT NULL AND thread_id = ?)) AND substr(sent_at, 1, 10) = ?",
                               (row["id"], row["from_addr"], row["thread_id"], today)).fetchone()
        if machine_sender(row["from_addr"], row["subject"]):
            conn.execute("UPDATE recruiter_emails SET state = 'DISMISSED' WHERE id = ?", (row["id"],))
            log.info("Not replying to %s (%s): an automatic mailer.", row["from_addr"], row["subject"])
            continue
        if row["from_addr"] == own or replied:
            out["waiting"] += 1  # already answered this person today; you decide on a second reply
            continue
        if row["category"] in skip or "[FILL IN" in body or budget <= 0:
            out["waiting"] += 1
            continue
        res = send(conn, row["id"], body, smtp_factory=smtp_factory)
        if res["ok"]:
            out["sent"] += 1
            budget -= 1
            log.info("Replied to %s: %s", row["from_addr"], row["subject"])
        else:
            out["waiting"] += 1
            log.info("Reply to %s not sent: %s", row["from_addr"], res["error"])
    if out["sent"] or out["waiting"]:
        log.info("Recruiter replies: %d sent, %d waiting for you on the Recruiter emails tab.", out["sent"], out["waiting"])
    return out


def send(conn, email_id: int, body: str, smtp_factory=None) -> dict:
    """Send your (possibly edited) reply. Called only from the review page's Send button."""
    row = conn.execute("SELECT * FROM recruiter_emails WHERE id = ?", (email_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "email not found"}
    if row["state"] == "SENT":
        return {"ok": False, "error": "already sent"}
    if "[FILL IN" in body:
        return {"ok": False, "error": "the reply still has a [FILL IN ...] gap"}
    if machine_sender(row["from_addr"], row["subject"]):
        return {"ok": False, "error": f"{row['from_addr']} is an automatic mailer; replies there go nowhere. Nothing was sent."}
    addr, pw = os.environ.get("EMAIL_ADDRESS"), os.environ.get("EMAIL_APP_PASSWORD")
    if not (addr and pw):
        return {"ok": False, "error": "EMAIL_ADDRESS / EMAIL_APP_PASSWORD missing in .env"}
    msg = EmailMessage()
    msg["From"], msg["To"] = addr, row["from_addr"]
    subj = row["subject"] or ""
    msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    msg["In-Reply-To"] = msg["References"] = row["message_id"]
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(host, port, timeout=30))
    try:
        with factory() as s:
            s.login(addr, pw.replace(" ", ""))
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error": f"could not send: {exc}"}
    conn.execute("UPDATE recruiter_emails SET state = 'SENT', sent_at = ?, draft_reply = ? WHERE id = ?",
                 (db.now_iso(), body, email_id))
    return {"ok": True}
