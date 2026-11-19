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
