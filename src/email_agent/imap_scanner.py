"""Reads your job-search inbox (read-only) for job alerts and recruiter messages.

* Job alerts (LinkedIn, Indeed, Glassdoor, Himalayas, BrighterMonday, ...): every
  job link in the email becomes a job in the normal list, and goes through the
  same 24-hour rule, ranking and form filling as any other job.
* Recruiter messages: saved with a reply drafted from your profile. You send a
  reply only by pressing Send on the review page.

The inbox is opened read-only, so nothing is marked as read, moved or deleted.
Uses EMAIL_ADDRESS, EMAIL_APP_PASSWORD, IMAP_HOST, IMAP_PORT from .env.
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from ..discovery.common import html_to_text, new_job

log = logging.getLogger("jobsprint")

ALERT_SENDERS = ("linkedin.com", "indeed.com", "indeedemail.com", "glassdoor.com", "himalayas.app", "wellfound.com",
                 "weworkremotely.com", "remoteok.com", "jobicy.com", "brightermonday", "fuzu.com", "myjobmag",
                 "jobberman", "ziprecruiter.com", "otta.com", "welcometothejungle.com", "workable.com",
                 "remotive.com", "dice.com", "monster.com", "careerjet", "jooble", "talent.com")
ALERT_SUBJECT = re.compile(r"job alert|new jobs?\b|jobs? for you|recommended jobs|jobs? (that )?match|jobs? you may|"
                           r"is hiring|are hiring|new opportunit|top jobs|jobs? near", re.I)
AUTOMATED = re.compile(r"no-?reply|do-?not-?reply|notifications?@|alerts?@|jobalerts|mailer|newsletter|digest|"
                       r"updates?@|info@|news@|marketing", re.I)
RECRUITER = re.compile(r"\b(interview|recruit\w*|hiring|your (application|cv|resume|profile)|opportunit\w+|"
                       r"position|role|vacanc\w+|shortlist\w*|next steps?|assessment|offer)\b", re.I)
JOB_LINK = re.compile(r"linkedin\.com/(comm/)?jobs/view/|indeed\.com/(viewjob|rc/clk|pagead/clk|m/viewjob)|"
                      r"glassdoor\.[a-z.]+/(job-listing|partner/joblisting)|greenhouse\.io/|lever\.co/|ashbyhq\.com/|"
                      r"himalayas\.app/companies/[^/]+/jobs/|wellfound\.com/jobs/|workable\.com/|"
                      r"brightermonday\.co\.ke/listings/|weworkremotely\.com/remote-jobs/", re.I)
CATEGORIES = [("interview_request", re.compile(r"\binterview|schedule a (call|chat)|availability|calendly", re.I)),
              ("assessment", re.compile(r"assessment|take-?home|coding (test|challenge)|test link", re.I)),
              ("rejection", re.compile(r"unfortunately|not (be )?moving forward|other candidates|regret to", re.I)),
              ("offer", re.compile(r"\boffer letter|pleased to offer|job offer", re.I)),
              ("info_request", re.compile(r"\b(send|share|provide)\b.{0,40}\b(cv|resume|salary|expectations|availability)", re.I))]


def _dec(v) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _bodies(msg) -> tuple[str, str]:
    text, html = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            s = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_type() == "text/html":
            html += s
        elif part.get_content_type() == "text/plain":
            text += s
    return text, html


def _unwrap(href: str) -> str:
    """Follow redirect wrappers that carry the real link in a query parameter."""
    for _ in range(3):
        q = parse_qs(urlparse(href).query)
        inner = next((q[k][0] for k in ("url", "u", "dest", "redirect", "target") if k in q and q[k][0].startswith("http")), None)
        if not inner:
            break
        href = unquote(inner)
    return href


def _clean_url(href: str) -> str:
    href = _unwrap(href)
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?]*-)?(\d{6,})", href)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}"
    m = re.search(r"indeed\.com/.*[?&]jk=([0-9a-f]{8,})", href)
    if m:
        return f"https://www.indeed.com/viewjob?jk={m.group(1)}"
    u = urlparse(href)
    return f"{u.scheme}://{u.netloc}{u.path}"


def parse_message(raw: bytes, now: datetime | None = None) -> dict:
    msg = email.message_from_bytes(raw)
    now = now or datetime.now(timezone.utc)
    name, addr = parseaddr(_dec(msg.get("From")))
    try:
        received = parsedate_to_datetime(msg.get("Date"))
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
    except Exception:
        received = now
    text, html = _bodies(msg)
    return {"message_id": (msg.get("Message-ID") or "").strip() or f"{addr}|{msg.get('Date')}",
            "thread_id": (msg.get("In-Reply-To") or msg.get("References") or "").split(" ")[0].strip() or None,
            "from_addr": addr.lower(), "from_name": name, "subject": _dec(msg.get("Subject")),
            "received_at": received.isoformat(timespec="seconds"),
            "age_hours": max(0.0, (now - received).total_seconds() / 3600),
            "text": text or html_to_text(html), "html": html}


def is_job_alert(m: dict) -> bool:
    domain = m["from_addr"].split("@")[-1]
    return any(s in domain for s in ALERT_SENDERS) and (bool(ALERT_SUBJECT.search(m["subject"])) or len(alert_jobs(m)) >= 2)


def is_recruiter(m: dict, own_address: str = "") -> bool:
    if m["from_addr"] == own_address.lower() or AUTOMATED.search(m["from_addr"]):
        return False
    if any(s in m["from_addr"].split("@")[-1] for s in ALERT_SENDERS) and ALERT_SUBJECT.search(m["subject"]):
        return False
    return bool(RECRUITER.search(m["subject"] + " " + m["text"][:3000]))


def category(m: dict) -> str:
    blob = m["subject"] + " " + m["text"][:4000]
    return next((name for name, rx in CATEGORIES if rx.search(blob)), "other")


def alert_jobs(m: dict) -> list[dict]:
    """Job links in an alert email, with the link text as the title."""
    soup = BeautifulSoup(m["html"] or "", "html.parser")
    links = [(a.get("href") or "", a.get_text("\n", strip=True)) for a in soup.find_all("a")]
    if not links:
        links = [(u, "") for u in re.findall(r"https?://\S+", m["text"])]
    sender = m["from_addr"].split("@")[-1]
    out, seen = [], {}
    for href, text in links:
        real = _unwrap(href)
        if not JOB_LINK.search(real):
            continue
        url = _clean_url(real)
        # Alert cards put title, company and place on separate lines inside one link.
        parts = [" ".join(x.split()) for x in text.split("\n") if x.strip()]
        parts = [x for x in parts if not re.fullmatch(r"(new|promoted|actively recruiting|easy apply|\d+ (connections?|alumni).*|·)", x, re.I)]
        title = parts[0] if parts else ""
        company = parts[1] if len(parts) > 1 and len(parts[1]) < 80 else None
        location = parts[2] if len(parts) > 2 and len(parts[2]) < 80 else None
        if company and " · " in company:
            company, _, location = company.partition(" · ")
        if url in seen:
            if title and seen[url]["title"] == "Job from alert email" and len(title) < 120:
                seen[url].update(title=title, company=company or seen[url]["company"], location=location or seen[url]["location"])
            continue
        if re.fullmatch(r"(view|apply|see|more|view job|apply now|see all jobs?|view all jobs?)?", title, re.I):
            title, company, location = "", None, None
        job = new_job(source=f"email:{sender}", external_id=url.rsplit("/", 1)[-1], url=url,
                      title=title[:120] or "Job from alert email", company=company, location=location,
                      age_hours=m["age_hours"],
                      posted_at=m["received_at"], description="")
        seen[url] = job
        out.append(job)
    return [j for j in out if j["title"] != "Job from alert email" or "linkedin" in j["url"]]


def fetch(lookback_hours: float = 24, limit: int = 300) -> tuple[list[dict], list[dict]]:
    """Return (jobs from alert emails, recruiter messages). Empty lists if email isn't set up."""
    addr, pw = os.environ.get("EMAIL_ADDRESS"), os.environ.get("EMAIL_APP_PASSWORD")
    if not (addr and pw):
        log.info("Email not set up (EMAIL_ADDRESS / EMAIL_APP_PASSWORD in .env), so the inbox was skipped.")
        return [], []
    host, port = os.environ.get("IMAP_HOST", "imap.gmail.com"), int(os.environ.get("IMAP_PORT", "993"))
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%d-%b-%Y")
    jobs, recruiters, n = [], [], 0
    try:
        with imaplib.IMAP4_SSL(host, port) as imap:
            imap.login(addr, pw.replace(" ", ""))
            imap.select("INBOX", readonly=True)
            typ, data = imap.search(None, "SINCE", since)
            ids = (data[0] or b"").split()[-limit:]
            for i in ids:
                typ, parts = imap.fetch(i, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if not raw:
                    continue
                m = parse_message(raw)
                if m["age_hours"] > lookback_hours:
                    continue
                n += 1
                if is_job_alert(m):
                    jobs.extend(alert_jobs(m))
                elif is_recruiter(m, addr):
                    m["category"] = category(m)
                    recruiters.append(m)
    except (imaplib.IMAP4.error, OSError) as exc:
        log.warning("Could not read the inbox at %s: %s. Check EMAIL_ADDRESS and EMAIL_APP_PASSWORD in .env.", host, exc)
        return [], []
    log.info("Inbox: read %d emails from the last %d hours: %d jobs from alerts, %d recruiter messages.",
             n, lookback_hours, len(jobs), len(recruiters))
    return jobs, recruiters
