"""Runs discovery end to end: fetch, filter, rank, enrich, save, report."""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime

from .. import database as db
from ..cv_tailor.single_template_tailor import load_profile
from ..intelligence.job_ranker import rank
from . import ai_platforms, ats_api_fetcher, jobspy_fetcher, remote_boards
from .common import load_yaml, log


def run(ws, run_id: int | None = None, sources=("jobspy", "ats", "remote")) -> dict:
    profile = load_profile()
    search_cfg = load_yaml("search_queries.yaml")
    platform_cfg = load_yaml("ai_platforms.yaml")

    raw = []
    if "ats" in sources:
        raw += ats_api_fetcher.fetch(platform_cfg)
    if "remote" in sources:
        raw += remote_boards.fetch(search_cfg.get("remote_boards") or remote_boards.FEEDS.keys())
    if "jobspy" in sources:
        raw += jobspy_fetcher.fetch(search_cfg)
    log.info("Found %d postings in total. Applying the 24-hour rule and knockouts...", len(raw))

    ranked = rank(raw, profile, search_cfg, platform_cfg)
    top_n = int(search_cfg.get("jobspy", {}).get("linkedin_details_top_n", 60))
    if top_n and any("linkedin" in (j.get("source") or "") for j in ranked):
        qualified = [j for j in ranked if j["status"] == "QUALIFIED"]
        rest = [j for j in ranked if j["status"] != "QUALIFIED"]
        pause = float(search_cfg.get("jobspy", {}).get("request_pause_seconds", 2.0))
        enriched = jobspy_fetcher.enrich_linkedin(qualified, top_n, pause)
        ranked = rank(enriched, profile, search_cfg, platform_cfg) + rest

    with db.session(ws.db_path) as conn:
        for j in ranked:
            if j.get("url"):
                db.upsert_job(conn, j, run_id)
        platforms = ai_platforms.sync_platforms(conn, platform_cfg)

    qualified = [j for j in ranked if j["status"] == "QUALIFIED"]
    def _why(j):
        r = j.get("status_reason") or ""
        if j["status"] == "REJECTED_STALE":
            return "older than 24 hours" if r.startswith("posted") else r
        return r.split("'")[0].split("(")[0].strip()
    reasons = Counter((j["status"], _why(j)) for j in ranked if j["status"] != "QUALIFIED")
    export = ws.exports / f"jobs_{datetime.now():%Y-%m-%d_%H%M}.csv"
    with open(export, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["priority", "fit", "age_hours", "applicants", "track", "title", "company", "location", "remote", "source", "url"])
        for j in qualified:
            w.writerow([j["priority_score"], j["fit_score"], round(j["age_hours"], 1), j.get("applicant_count"),
                        j["track"], j["title"], j.get("company"), j.get("location"), j["is_remote"], j["source"], j["url"]])

    log.info("Kept %d qualified jobs out of %d. Saved the list to %s", len(qualified), len(ranked), export)
    for (status, why), n in reasons.most_common(12):
        log.info("  dropped %3d  %s: %s", n, status, why or "-")
    print()
    print(f"{'#':>2}  {'prio':>4}  {'fit':>4}  {'age':>4}  {'appl':>5}  {'track':<10}  title @ company")
    for i, j in enumerate(qualified[:20], 1):
        appl = "?" if j.get("applicant_count") is None else str(j["applicant_count"])
        print(f"{i:>2}  {j['priority_score']:.2f}  {j['fit_score']:.2f}  {j['age_hours']:>3.0f}h  {appl:>5}  {j['track']:<10}  "
              f"{j['title'][:60]} @ {(j.get('company') or '?')[:30]}")
    todo = [p["name"] for p in platforms if p["status"] == "NOT_APPLIED"]
    if todo:
        print(f"\nAI platforms you haven't marked as applied: {', '.join(todo)}")
    return {"found": len(raw), "kept": len(qualified), "export": str(export)}
