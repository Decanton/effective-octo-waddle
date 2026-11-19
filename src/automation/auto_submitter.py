"""Sends complete applications on its own, inside the limits in config/agent_settings.yaml.

A form is sent only when ALL of these hold:
  * it is READY_TO_SUBMIT (every required question answered, CV attached);
  * it is on an allowed application system (Greenhouse, Lever, Ashby by default);
  * it has no CAPTCHA;
  * it has no required consent box, unless accept_required_consents is true;
  * today's sent count is under max_per_day.
Everything else stays on the review page for you.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from .. import database as db
from ..settings import load_settings

log = logging.getLogger("jobsprint")


def sent_today(conn) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return conn.execute("SELECT COUNT(*) FROM staged_forms WHERE state IN ('SUBMITTED','SENT_UNCONFIRMED') AND substr(submitted_at, 1, 10) = ?",
                        (today,)).fetchone()[0]


def eligible(row, cfg: dict) -> tuple[bool, str, list[str]]:
    """(ok, reason, consent labels to tick) for one staged form row."""
    if row["state"] != "READY_TO_SUBMIT":
        return False, "not complete", []
    host = urlparse(row["form_url"] or "").netloc.lower()
    if not any(h in host for h in cfg["only_hosts"]):
        return False, f"{host or 'unknown site'} is not on the auto-send list", []
    if (row["error"] or "").lower().startswith("captcha"):
        return False, "has a CAPTCHA", []
    answers = json.loads(row["answers_json"] or "[]")
    consents = [a["label"] for a in answers if a.get("consent") and a.get("required")]
    if consents and not cfg["accept_required_consents"]:
        return False, "has a required consent box (accept_required_consents is off)", []
    return True, "", consents


def run(ws, settings: dict | None = None, submit_fn=None) -> dict:
    cfg = (settings or load_settings())["auto_submit"]
    counts = {"sent": 0, "not_confirmed": 0, "failed": 0, "waiting_for_you": 0}
    if not cfg["enabled"]:
        log.info("Automatic sending is off (config/agent_settings.yaml). Ready forms wait on the review page.")
        return counts
    if submit_fn is None:
        from .form_submitter import submit as submit_fn
    with db.session(ws.db_path) as conn:
        budget = int(cfg["max_per_day"]) - sent_today(conn)
        rows = conn.execute("SELECT s.*, j.title, j.company FROM staged_forms s JOIN jobs j ON j.id = s.job_id "
                            "WHERE s.state = 'READY_TO_SUBMIT' ORDER BY j.priority_score DESC").fetchall()
    if budget <= 0:
        log.info("Daily limit of %s sent applications reached; the rest wait for tomorrow or for you.", cfg["max_per_day"])
        return counts
    for row in rows:
        ok, why, consents = eligible(row, cfg)
        if not ok:
            counts["waiting_for_you"] += 1
            log.info("Waiting for you: %s @ %s (%s)", row["title"][:60], row["company"], why)
            continue
        if budget <= 0:
            counts["waiting_for_you"] += 1
            continue
        log.info("Sending: %s @ %s", row["title"][:60], row["company"])
        try:
            res = submit_fn(ws, row["id"], None, consents, confirm_wait_ms=int(cfg["confirm_wait_seconds"]) * 1000)
        except Exception as exc:  # one bad form must not stop the rest
            log.warning("Sending failed for %s: %s", row["title"], exc)
            counts["failed"] += 1
            continue
        if res.get("ok"):
            counts["sent"] += 1
            budget -= 1
        elif "no confirmation" in (res.get("error") or ""):
            counts["not_confirmed"] += 1
            budget -= 1  # it was probably sent; count it so the cap holds
        else:
            counts["failed"] += 1
            log.info("  not sent: %s", res.get("error"))
    log.info("Automatic sending done: %d sent, %d sent but no thank-you page seen, %d failed, %d waiting for you.",
             counts["sent"], counts["not_confirmed"], counts["failed"], counts["waiting_for_you"])
    return counts
