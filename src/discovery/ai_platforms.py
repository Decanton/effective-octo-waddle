"""AI training and evaluation platforms.

Platforms like AfterQuery, Outlier, Alignerr, DataAnnotation and Mercor run
one standing application instead of dated job posts, so they can't pass a
24-hour freshness check. They are tracked as 'evergreen' platforms: the agent
lists the ones you haven't applied to yet, and you mark them done once.
Companies with dated public boards are fetched in ats_api_fetcher.py.
"""
from __future__ import annotations

from .common import load_yaml
from .. import database as db


def sync_platforms(conn, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_yaml("ai_platforms.yaml")
    for p in cfg.get("evergreen_platforms", []):
        conn.execute(
            "INSERT INTO platforms(name, url, track, status, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET url = excluded.url, track = excluded.track",
            (p["name"], p["url"], p.get("track"), "NOT_APPLIED", db.now_iso()))
    return [dict(r) for r in conn.execute("SELECT * FROM platforms ORDER BY status, name")]


def mark_platform(conn, name: str, status: str) -> None:
    if status not in ("NOT_APPLIED", "APPLIED", "ACCEPTED", "IGNORED"):
        raise ValueError(status)
    conn.execute("UPDATE platforms SET status = ?, updated_at = ? WHERE name = ?", (status, db.now_iso(), name))
