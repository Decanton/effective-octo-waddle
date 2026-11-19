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


def _cf_decode(hexstr: str) -> str:
    """Cloudflare hides emails on many job blogs as data-cfemail="..."; this undoes it."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def decode_emails(html: str) -> str:
    html = re.sub(r"<[^>]+data-cfemail=[\"']([0-9a-fA-F]+)[\"'][^>]*>.*?</[^>]+>", lambda m: _cf_decode(m.group(1)), html, flags=re.S)
    return re.sub(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)", lambda m: "mailto:" + _cf_decode(m.group(1)), html)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    text = unescape(decode_emails(html))
    text = re.sub(r"<a[^>]+href=[\"']mailto:([^\"'?]+)[^>]*>(?!\s*\1)", lambda m: m.group(0) + f" {m.group(1)} ", text, flags=re.I)
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
