"""Waits for a verification email and pulls out the code or activation link.

Reads the inbox read-only (EMAIL_ADDRESS / EMAIL_APP_PASSWORD from .env). Only emails
that arrived after `since` are considered, newest first.
"""
from __future__ import annotations

import imaplib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from .imap_scanner import parse_message

log = logging.getLogger("jobsprint")

CODE_NEAR = re.compile(r"(code|otp|one.?time|verification|verify|passcode|pin|security)", re.I)
CODE = re.compile(r"(?<![\d-])(\d{6}|\d{3}[ -]\d{3}|[A-Z0-9]{6})(?![\d-])")
LINK_WORDS = re.compile(r"verify|confirm|activate|activation|validate|magic|sign.?in|login|token", re.I)


def extract(m: dict, host_hint: str = "") -> dict | None:
    """{'code': '123456'} or {'link': 'https://...'} from one parsed email, or None."""
    text = (m.get("subject") or "") + "\n" + (m.get("text") or "")
    for match in CODE.finditer(text):
        window = text[max(0, match.start() - 160): match.end() + 40]
        code = match.group(1).replace(" ", "").replace("-", "")
        if CODE_NEAR.search(window) and (code.isdigit() or (any(c.isdigit() for c in code) and any(c.isalpha() for c in code))):
            return {"code": code}
    soup = BeautifulSoup(m.get("html") or "", "html.parser")
    links = [(a.get("href") or "", a.get_text(" ", strip=True)) for a in soup.find_all("a")]
    links += [(u, "") for u in re.findall(r"https?://\S+", m.get("text") or "")]
    base = host_hint.split(":")[0].split(".")[-2] if host_hint.count(".") >= 1 else host_hint
    best = None
    for href, label in links:
        if not href.startswith("http") or "unsubscribe" in href.lower():
            continue
        if LINK_WORDS.search(href + " " + label):
            score = 2 if base and base in href else 1
            if best is None or score > best[0]:
                best = (score, href.rstrip(").,>\"'"))
    return {"link": best[1]} if best else None


def relevant(m: dict, host_hint: str) -> bool:
    if not host_hint:
        return True
    name = host_hint.split(":")[0].lower().replace("www.", "")
    parts = [p for p in name.split(".") if p not in ("com", "co", "io", "net", "org", "jobs", "careers", "apply", "ke", "uk", "de", "hr")]
    blob = f"{m.get('from_addr')} {m.get('from_name')} {m.get('subject')} {(m.get('text') or '')[:2000]}".lower()
    return any(p and p in blob for p in parts) or bool(CODE_NEAR.search(m.get("subject") or ""))


def wait_for_code(since: datetime, host_hint: str = "", timeout: float = 60, poll: float = 5, fetch=None) -> dict | None:
    """Poll the inbox until a matching verification email arrives, or give up after `timeout` seconds."""
    fetch = fetch or _recent
    deadline = time.monotonic() + timeout
    while True:
        for m in fetch(since):
            if relevant(m, host_hint):
                found = extract(m, host_hint)
                if found:
                    log.info("Found the verification %s from %s", "code" if "code" in found else "link", m.get("from_addr"))
                    return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def _recent(since: datetime) -> list[dict]:
    addr, pw = os.environ.get("EMAIL_ADDRESS"), os.environ.get("EMAIL_APP_PASSWORD")
    if not (addr and pw):
        return []
    host, port = os.environ.get("IMAP_HOST", "imap.gmail.com"), int(os.environ.get("IMAP_PORT", "993"))
    out = []
    try:
        with imaplib.IMAP4_SSL(host, port) as imap:
            imap.login(addr, pw.replace(" ", ""))
            for box in ("INBOX", "[Gmail]/Spam"):
                if imap.select(f'"{box}"', readonly=True)[0] != "OK":
                    continue
                day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
                typ, data = imap.search(None, "SINCE", day)
                for i in reversed((data[0] or b"").split()[-30:]):
                    typ, parts = imap.fetch(i, "(BODY.PEEK[])")
                    raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                    if raw:
                        m = parse_message(raw)
                        if datetime.fromisoformat(m["received_at"]) >= since - timedelta(seconds=90):
                            out.append(m)
    except (imaplib.IMAP4.error, OSError) as exc:
        log.warning("Could not read the inbox for a verification code: %s", exc)
    return out


def now() -> datetime:
    return datetime.now(timezone.utc)
