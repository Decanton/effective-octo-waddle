"""SQLite schema and queries for the job sprint agent."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sprint_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    mode          TEXT NOT NULL,
    jobs_found    INTEGER DEFAULT 0,
    jobs_kept     INTEGER DEFAULT 0,
    jobs_staged   INTEGER DEFAULT 0,
    emails_found  INTEGER DEFAULT 0,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    external_id     TEXT,
    url             TEXT NOT NULL UNIQUE,
    apply_url       TEXT,
    title           TEXT NOT NULL,
    company         TEXT,
    location        TEXT,
    is_remote       INTEGER,
    description     TEXT,
    posted_at       TEXT,
    age_hours       REAL,
    applicant_count INTEGER,
    track           TEXT,
    fit_score       REAL,
    priority_score  REAL,
    status          TEXT NOT NULL DEFAULT 'NEW',
    status_reason   TEXT,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    sprint_run_id   INTEGER REFERENCES sprint_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_priority ON jobs(priority_score DESC);

CREATE TABLE IF NOT EXISTS tailored_cvs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    track            TEXT NOT NULL,
    docx_path        TEXT NOT NULL,
    matched_keywords TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staged_forms (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cv_id          INTEGER REFERENCES tailored_cvs(id),
    form_url       TEXT,
    answers_json   TEXT,
    missing_json   TEXT,
    state          TEXT NOT NULL DEFAULT 'STAGING',
    error          TEXT,
    created_at     TEXT NOT NULL,
    submitted_at   TEXT,
    receipt_json   TEXT
);

CREATE TABLE IF NOT EXISTS portal_credentials (
    host        TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    secret      TEXT NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS screenshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    staged_form_id INTEGER NOT NULL REFERENCES staged_forms(id) ON DELETE CASCADE,
    path           TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'before_submit',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platforms (
    name        TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    track       TEXT,
    status      TEXT NOT NULL DEFAULT 'NOT_APPLIED',
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS recruiter_emails (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     TEXT NOT NULL UNIQUE,
    thread_id      TEXT,
    from_addr      TEXT NOT NULL,
    from_name      TEXT,
    subject        TEXT,
    received_at    TEXT,
    body_text      TEXT,
    category       TEXT,
    confidence     REAL,
    draft_reply    TEXT,
    state          TEXT NOT NULL DEFAULT 'NEW',
    sent_at        TEXT,
    created_at     TEXT NOT NULL
);
"""

# Allowed job statuses, in the order a job normally moves through them.
JOB_STATUSES = ("NEW", "REJECTED_STALE", "REJECTED_KNOCKOUT", "QUALIFIED", "CV_READY",
                "STAGING", "READY_TO_SUBMIT", "NEEDS_USER_INPUT", "NEEDS_LOGIN", "NEEDS_MANUAL_OTP", "MANUAL_APPLY",
                "SUBMITTED", "SKIPPED", "FAILED")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(staged_forms)")}
        if "form_url" not in cols:  # upgrade a database made by an earlier step
            conn.execute("ALTER TABLE staged_forms ADD COLUMN form_url TEXT")
        if "receipt_json" not in cols:
            conn.execute("ALTER TABLE staged_forms ADD COLUMN receipt_json TEXT")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


@contextmanager
def session(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def start_run(conn, mode: str) -> int:
    cur = conn.execute("INSERT INTO sprint_runs(started_at, mode) VALUES (?, ?)", (now_iso(), mode))
    return cur.lastrowid


def finish_run(conn, run_id: int, **counts) -> None:
    cols = {k: v for k, v in counts.items() if k in {"jobs_found", "jobs_kept", "jobs_staged", "emails_found", "notes"}}
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE sprint_runs SET finished_at = ?{', ' + sets if sets else ''} WHERE id = ?",
                 (now_iso(), *cols.values(), run_id))


def upsert_job(conn, job: dict, run_id: int | None = None) -> int:
    """Insert a job, or update it if the URL is already known. Returns the job id."""
    ts = now_iso()
    fields = ["source", "external_id", "apply_url", "title", "company", "location", "is_remote",
              "description", "posted_at", "age_hours", "applicant_count", "track", "fit_score",
              "priority_score", "status", "status_reason"]
    vals = {k: job.get(k) for k in fields}
    vals["status"] = vals["status"] or "NEW"
    row = conn.execute("SELECT id FROM jobs WHERE url = ?", (job["url"],)).fetchone()
    if row:
        sets = ", ".join(f"{k} = COALESCE(?, {k})" for k in fields)
        conn.execute(f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?", (*vals.values(), ts, row["id"]))
        return row["id"]
    cols = ", ".join(["url", *fields, "first_seen_at", "updated_at", "sprint_run_id"])
    qs = ", ".join("?" * (len(fields) + 4))
    cur = conn.execute(f"INSERT INTO jobs({cols}) VALUES ({qs})", (job["url"], *vals.values(), ts, ts, run_id))
    return cur.lastrowid


def set_job_status(conn, job_id: int, status: str, reason: str | None = None) -> None:
    if status not in JOB_STATUSES:
        raise ValueError(f"Unknown status {status}")
    conn.execute("UPDATE jobs SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                 (status, reason, now_iso(), job_id))


def count_by_status(conn) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


def add_tailored_cv(conn, job_id: int, track: str, docx_path: str, matched_keywords: list[str]) -> int:
    cur = conn.execute("INSERT INTO tailored_cvs(job_id, track, docx_path, matched_keywords, created_at) VALUES (?,?,?,?,?)",
                       (job_id, track, docx_path, ", ".join(matched_keywords), now_iso()))
    return cur.lastrowid
