"""Shared helpers for the job fetchers."""
from __future__ import annotations

import logging
import re
from html import unescape
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("jobsprint")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def load_yaml(name: str) -> dict:
    with open(ROOT / "config" / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    text = unescape(html)
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def new_job(**kw) -> dict:
    """Normalise a job record so every fetcher returns the same fields."""
    keys = ("source", "external_id", "url", "apply_url", "title", "company", "location",
            "is_remote", "description", "posted_at", "age_hours", "applicant_count", "track")
    job = {k: kw.get(k) for k in keys}
    job["title"] = " ".join(unescape(job["title"] or "").split())
    if job["company"]:
        job["company"] = unescape(job["company"]).strip()
    job["apply_url"] = job["apply_url"] or job["url"]
    if job["is_remote"] is None:
        blob = f"{job['title']} {job['location'] or ''}".lower()
        job["is_remote"] = any(w in blob for w in ("remote", "anywhere", "worldwide", "work from home"))
    job["is_remote"] = bool(job["is_remote"])
    return job
