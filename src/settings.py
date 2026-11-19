"""Reads config/agent_settings.yaml (how far the agent acts on its own)."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "auto_submit": {"enabled": False, "max_per_day": 20, "only_hosts": ["greenhouse.io", "lever.co", "ashbyhq.com"],
                    "accept_required_consents": False, "confirm_wait_seconds": 60},
    "email": {"enabled": True, "lookback_hours": 24},
    "email_applications": {"enabled": False, "max_per_day": 10},
    "recruiter_replies": {"auto_send": False, "max_per_day": 10, "skip_categories": ["rejection", "offer"]},
    "google_sign_in": {"enabled": False, "allow_new_accounts": False},
    "portal_accounts": {"enabled": False, "accept_terms": False, "code_wait_seconds": 60},
    "update": {"enabled": True, "on_run": True, "check_minutes": 60, "run_tests": True, "branch": "main",
               "repo": "https://github.com/Decanton/effective-octo-waddle.git"},
    "human_assist": {"enabled": True, "attended": False, "wait_seconds": 120, "sms_wait_seconds": 10, "keep_messages_open": True,
                     "pacing": {"between_steps_seconds": [1.5, 4.0], "between_fields_seconds": [0.2, 0.6]}},
    "browser": {"use_cdp": True, "cdp_port": 9222},
}


def load_settings(path: Path | None = None) -> dict:
    path = path or ROOT / "config" / "agent_settings.yaml"
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = {}
    for section, defaults in DEFAULTS.items():
        out[section] = {**defaults, **(data.get(section) or {})}
    return out
