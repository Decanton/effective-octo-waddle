"""Results: how many jobs were found, applied to and answered, and which applications need a follow-up."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import database as db

POSITIVE = ("interview_request", "interview", "next_steps", "assessment")


def _domain(addr: str | None) -> str:
    return (addr or "").split("@")[-1].lower().strip(">") if addr and "@" in addr else ""


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("ltd", "").replace("limited", "").replace("inc", ""))


def replies_for(conn, app: dict, mails: list[dict]) -> list[dict]:
    """Emails from the company after the application was sent."""
    to = (app.get("form_url") or "")[7:] if (app.get("form_url") or "").startswith("mailto:") else ""
    comp = _norm(app.get("company"))
    out = []
    for m in mails:
        if app.get("submitted_at") and (m.get("received_at") or "") < app["submitted_at"]:
            continue
        dom = _domain(m.get("from_addr"))
        if (to and dom and dom == _domain(to)) or (comp and len(comp) > 3 and (comp in _norm(dom) or comp in _norm(m.get("from_name"))
                                                                              or comp in _norm(m.get("subject")))):
            out.append(m)
    return out


def results(conn, days: int = 30, follow_after: int = 7) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    found = conn.execute("SELECT COUNT(*) FROM jobs WHERE first_seen_at >= ?", (since,)).fetchone()[0]
    qualified = conn.execute("SELECT COUNT(*) FROM jobs WHERE first_seen_at >= ? AND status NOT LIKE 'REJECTED%' AND status != 'NEW'",
                             (since,)).fetchone()[0]
    staged = conn.execute("SELECT COUNT(DISTINCT job_id) FROM staged_forms WHERE created_at >= ?", (since,)).fetchone()[0]
    apps = [dict(r) for r in conn.execute(
        "SELECT s.id, s.form_url, s.submitted_at, s.state, s.receipt_json, j.title, j.company, j.source FROM staged_forms s "
        "JOIN jobs j ON j.id = s.job_id WHERE s.state IN ('SUBMITTED','SENT_UNCONFIRMED') AND s.submitted_at >= ? "
        "ORDER BY s.submitted_at DESC", (since,))]
    mails = [dict(r) for r in conn.execute("SELECT id, from_addr, from_name, subject, category, received_at FROM recruiter_emails "
                                           "WHERE received_at >= ?", (since,))]
    followed = {r[0] for r in conn.execute("SELECT staged_form_id FROM followups")}
    due_before = (datetime.now(timezone.utc) - timedelta(days=follow_after)).isoformat(timespec="seconds")
    replied = interviews = rejected = offers = 0
    rows = []
    for a in apps:
        rs = replies_for(conn, a, mails)
        cats = {m.get("category") for m in rs}
        status = ("offer" if "offer" in cats else "interview" if cats & set(POSITIVE) else
                  "rejected" if "rejection" in cats else "replied" if rs else "waiting")
        replied += bool(rs)
        interviews += status == "interview"
        rejected += status == "rejected"
        offers += status == "offer"
        due = status == "waiting" and a["submitted_at"] < due_before and a["id"] not in followed
        rows.append({"id": a["id"], "title": a["title"], "company": a["company"], "source": a["source"],
                     "method": "email" if (a["form_url"] or "").startswith("mailto:") else "form",
                     "submitted_at": a["submitted_at"], "status": status, "followed_up": a["id"] in followed,
                     "follow_up_due": due, "replies": [{"subject": m["subject"], "from": m["from_addr"], "at": m["received_at"]} for m in rs]})
    by_source = {}
    for r in rows:
        s = by_source.setdefault(r["source"] or "?", {"sent": 0, "replied": 0})
        s["sent"] += 1
        s["replied"] += r["status"] != "waiting"
    sent = len(apps)
    return {"days": days, "funnel": {"found": found, "qualified": qualified, "staged": staged, "sent": sent, "replied": replied,
                                     "interviews": interviews, "rejected": rejected, "offers": offers},
            "response_rate": round(replied / sent, 3) if sent else None,
            "by_source": by_source, "applications": rows}
