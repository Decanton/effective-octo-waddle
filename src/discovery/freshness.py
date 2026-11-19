"""Deterministic posting-age and applicant-count parsing.

The 24-hour rule is enforced here, in code, not by an LLM:
  - parse_age_hours() turns whatever a source gives (ISO timestamp, epoch
    milliseconds, a date, or text like "2 hours ago") into hours since posting.
  - is_fresh() accepts a job only when that age is known and <= the limit.
A missing or unparseable date means the job is rejected.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

MAX_AGE_HOURS = 24.0

_UNIT_HOURS = {
    "second": 1 / 3600, "sec": 1 / 3600, "s": 1 / 3600,
    "minute": 1 / 60, "min": 1 / 60, "m": 1 / 60,
    "hour": 1.0, "hr": 1.0, "h": 1.0,
    "day": 24.0, "d": 24.0,
    "week": 168.0, "wk": 168.0, "w": 168.0,
    "month": 720.0, "mo": 720.0,
    "year": 8760.0, "yr": 8760.0, "y": 8760.0,
}
_JUST_NOW = re.compile(r"\b(just posted|just now|moments ago|posted today|today|few seconds ago|few minutes ago)\b")
_RELATIVE = re.compile(
    r"\b(?P<num>\d+|an?(?=\s)|one(?=\s))\+?\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?|[smhdwy])\b\s*(ago)?",
    re.I)


def _now(now: datetime | None) -> datetime:
    return now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)


def parse_age_hours(value, now: datetime | None = None, date_only_ok: bool = False) -> float | None:
    """Return hours since posting, or None when it can't be determined.

    date_only_ok: a bare date (no time) is normally rejected because it can't
    prove <= 24h. Set it when the source already filtered to the last 24 hours
    on its side (for example LinkedIn's f_TPR filter); then today's or
    yesterday's date is accepted.
    """
    now = _now(now)
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN from pandas
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 3600)
    if isinstance(value, date):
        return _age_from_date(value, now, date_only_ok)
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value  # epoch ms or s
        return max(0.0, (now.timestamp() - secs) / 3600)

    text = str(value).strip()
    if not text:
        return None
    low = text.lower()
    if re.fullmatch(r"\d{10,13}", text):
        return parse_age_hours(int(text), now)
    try:  # ISO 8601 with time
        if "t" in low or re.search(r"\d:\d", text):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
            return parse_age_hours(dt, now)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return _age_from_date(date.fromisoformat(text), now, date_only_ok)
    except ValueError:
        pass
    if _JUST_NOW.search(low):
        return 0.0
    if "yesterday" in low:
        return 24.0 if date_only_ok else None
    m = _RELATIVE.search(low)
    if m:
        num = m.group("num").lower()
        n = 1 if num in ("a", "an", "one") else int(num)
        unit = m.group("unit").lower()
        unit = unit if unit in _UNIT_HOURS else unit.rstrip("s")
        hours = _UNIT_HOURS.get(unit)
        if hours is not None:
            plus = "+" in low[m.start():m.end()]
            return n * hours + (1 if plus else 0)
    return None


def _age_from_date(d: date, now: datetime, date_only_ok: bool) -> float | None:
    today = now.date()
    if d > today:
        return 0.0
    if not date_only_ok:
        return None
    if d == today:
        start = datetime.combine(d, time.min, tzinfo=timezone.utc)
        return max(0.0, (now - start).total_seconds() / 3600)
    if d == today - timedelta(days=1):
        return 24.0
    return (today - d).days * 24.0


def is_fresh(age_hours: float | None, max_age: float = MAX_AGE_HOURS) -> bool:
    return age_hours is not None and 0 <= age_hours <= max_age


_APPLICANTS = [
    (re.compile(r"be among the first\s+(\d+)\s+applicants", re.I), lambda m: int(m.group(1)) - 1),
    (re.compile(r"over\s+([\d,]+)\s+applicants", re.I), lambda m: int(m.group(1).replace(",", "")) + 1),
    (re.compile(r"([\d,]+)\+?\s+applicants?", re.I), lambda m: int(m.group(1).replace(",", ""))),
    (re.compile(r"([\d,]+)\+?\s+people\s+(?:have\s+)?(?:applied|clicked apply)", re.I), lambda m: int(m.group(1).replace(",", ""))),
    (re.compile(r"applicants?:\s*([\d,]+)", re.I), lambda m: int(m.group(1).replace(",", ""))),
    (re.compile(r"\bno applicants yet\b|\bbe the first to apply\b", re.I), lambda m: 0),
]


def parse_applicants(text) -> int | None:
    """Turn text like 'Over 200 applicants' or 'Be among the first 25 applicants' into a number."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and text == text:
        return int(text)
    s = " ".join(str(text).split())
    for rx, fn in _APPLICANTS:
        m = rx.search(s)
        if m:
            return max(0, fn(m))
    return None
