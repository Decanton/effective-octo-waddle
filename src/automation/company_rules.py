"""Per-company rules: never apply to one role twice, and respect a company's application limit.

Some companies say how many applications they accept (Supabase: 3 in 60 days; ElevenLabs: 2 in
90 days). Everyone else gets company_limits.default from config/agent_settings.yaml, so one
company never gets a flood of applications. Names are compared loosely ("Acme Ltd" = "ACME").
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

SUFFIX = re.compile(r"\b(ltd|limited|inc|incorporated|llc|plc|gmbh|corp|corporation|co|company|s\.?a|b\.?v|pty)\b\.?", re.I)
SENT = ("SUBMITTED", "SENT_UNCONFIRMED")


def norm_company(name: str | None) -> str:
    n = SUFFIX.sub(" ", (name or "").lower().replace("&", " and "))
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def norm_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _sent_rows(conn, exclude_job: int | None = None):
    return conn.execute(
        "SELECT j.id, j.company, j.title, COALESCE(s.submitted_at, s.created_at) AS at FROM staged_forms s "
        "JOIN jobs j ON j.id = s.job_id WHERE s.state IN ('SUBMITTED','SENT_UNCONFIRMED') AND j.id != ?",
        (exclude_job or -1,)).fetchall()


def already_applied(conn, job: dict) -> bool:
    """The same role at the same company was already sent (from any site or by email)."""
    c, t = norm_company(job.get("company")), norm_title(job.get("title"))
    if not c or not t:
        return False
    return any(norm_company(r["company"]) == c and norm_title(r["title"]) == t for r in _sent_rows(conn, job.get("id")))


def limit_for(company: str | None, cfg: dict | None) -> tuple[int, int]:
    cfg = cfg or {}
    key = norm_company(company)
    for name, rule in (cfg.get("per_company") or {}).items():
        if norm_company(name) == key:
            return int(rule.get("max", 3)), int(rule.get("days", 60))
    d = cfg.get("default") or {}
    return int(d.get("max", 3)), int(d.get("days", 60))


def sent_recently(conn, company: str | None, days: int) -> int:
    c = norm_company(company)
    if not c:
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return sum(1 for r in _sent_rows(conn) if norm_company(r["company"]) == c and (r["at"] or "") >= since)


def blocked(conn, job: dict, cfg: dict | None, extra_sent: int = 0) -> str | None:
    """Why this job must not be sent, or None."""
    if already_applied(conn, job):
        return "already applied to this role (found again on another site)"
    if not job.get("company"):
        return None
    mx, days = limit_for(job.get("company"), cfg)
    n = sent_recently(conn, job.get("company"), days) + extra_sent
    if n >= mx:
        return f"{job['company']} limit: {n} sent in {days} days (max {mx})"
    return None


def prune_ready(conn, cfg: dict | None) -> int:
    """Forms waiting on the review page that must not go: repeats of a role already sent, and those
    over a company's limit (the best-ranked ones are kept). Marked skipped, with the reason."""
    rows = conn.execute(
        "SELECT s.id, s.job_id, j.company, j.title FROM staged_forms s JOIN jobs j ON j.id = s.job_id "
        "WHERE s.state = 'READY_TO_SUBMIT' AND s.id = (SELECT MAX(id) FROM staged_forms WHERE job_id = s.job_id) "
        "ORDER BY j.priority_score DESC").fetchall()
    kept: dict[str, int] = {}
    seen, n = set(), 0
    for r in rows:
        c = norm_company(r["company"])
        key = (c, norm_title(r["title"]))
        why = None
        if c and key in seen:
            why = "same role as another form waiting for you"
        elif already_applied(conn, {"id": r["job_id"], "company": r["company"], "title": r["title"]}):
            why = "already applied to this role (found again on another site)"
        elif c:
            mx, days = limit_for(r["company"], cfg)
            if sent_recently(conn, r["company"], days) + kept.get(c, 0) >= mx:
                why = f"{r['company']} limit: at most {mx} applications in {days} days"
        if why:
            conn.execute("UPDATE staged_forms SET state = 'SKIPPED', error = ? WHERE id = ?", (why, r["id"]))
            conn.execute("UPDATE jobs SET status = 'SKIPPED', status_reason = ? WHERE id = ?", (why, r["job_id"]))
            n += 1
            continue
        seen.add(key)
        if c:
            kept[c] = kept.get(c, 0) + 1
    return n
