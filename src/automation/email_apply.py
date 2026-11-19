"""Applying by email: for jobs that say "send your CV and cover letter to hr@company.com".

1. Finds the application address and any requested subject line in the job text.
2. Builds the tailored CV (single master template) and a cover letter that uses only
   facts from your profile (an AI draft that is checked for invented numbers, or a
   plain template when no AI is available).
3. Saves it as a staged application (form_url "mailto:...") so it shows on the
   review page with the cover letter editable.
4. Sends it by SMTP from EMAIL_ADDRESS in .env, with the CV and cover letter attached:
   automatically within the limits in config/agent_settings.yaml (email_applications),
   or when you press Submit on the review page. Each job is emailed at most once.
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from .. import database as db
from .. import llm_client
from ..intelligence.answer_engine import _profile_facts, _unverified_numbers

log = logging.getLogger("jobsprint")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
APPLY_WORDS = re.compile(r"\b(send|e-?mail|submit|forward|apply|applications?|address(ed)?)\b", re.I)
DOC_WORDS = re.compile(r"\b(cv|c\.v\.|resume|résumé|curriculum vitae|cover(ing)? letter|application|applications|documents)\b", re.I)
BAD_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|privacy|gdpr|dpo|legal|abuse|support|help|press|media|"
                       r"accommodations?|accessibility|disability|webmaster|unsubscribe|security|billing|sales)", re.I)
BAD_DOMAIN = ("example.com", "example.org", "domain.com", "email.com", "sentry.io", "wixpress.com", "linkedin.com",
              "greenhouse.io", "lever.co", "ashbyhq.com", "indeed.com", "glassdoor.com",
              # job sites and blogs that list jobs: their own addresses never take applications
              "jobwebkenya.com", "corporatestaffing.co.ke", "careerpointkenya.co.ke", "opportunitiesforyoungkenyans.co.ke",
              "myjobmag.co.ke", "myjobmag.com", "brightermonday.co.ke", "fuzu.com", "kenyajob.com", "ngojobsinafrica.com",
              "weworkremotely.com", "himalayas.app", "jobicy.com", "remotive.com", "remoteok.com", "arbeitnow.com",
              "workingnomads.com", "workingnomads.co")
SUBJECT_RE = re.compile(r"(?:subject(?: line)?|title of (?:the|your) (?:e-?mail|mail))\s*(?:line)?\s*(?:should (?:be|read)|:|as|must be|of)?\s*"
                        r"[\"'“‘]?([^\"'”’\n]{4,120}?)[\"'”’]?(?:\.\s|\n|$|,)", re.I)


def find_apply_email(text: str) -> dict | None:
    """The address the job asks applications to be sent to, or None.

    The address must sit within ~200 characters of words like "send/email ... CV / application".
    Privacy, accessibility, support and no-reply addresses are never used.
    """
    if not text:
        return None
    best = None
    for m in EMAIL_RE.finditer(text):
        addr = m.group(0).rstrip(".").lower()
        local, _, domain = addr.partition("@")
        if BAD_LOCAL.search(local) or any(domain.endswith(d) for d in BAD_DOMAIN):
            continue
        window = text[max(0, m.start() - 220): m.end() + 80]
        if not (APPLY_WORDS.search(window) and DOC_WORDS.search(window)):
            continue
        if re.search(r"accommodat|disabilit|privacy|data protection|questions about", window, re.I) and \
                not re.search(r"\b(send|submit|forward)\b.{0,60}\b(cv|resume|application)", window, re.I):
            continue
        score = len(re.findall(r"\b(send|submit|forward|cv|resume|cover letter|apply)\b", window, re.I))
        if best is None or score > best[0]:
            best = (score, addr, m.start())
    if not best:
        return None
    near = text[max(0, best[2] - 400): best[2] + 400]
    quoted = re.search(r"subject(?: line)?[^\"“'\n]{0,40}[\"“]([^\"”\n]{4,120})[\"”]", near, re.I)
    subj = quoted or SUBJECT_RE.search(near)
    return {"to": best[1], "subject_hint": subj.group(1).strip() if subj else None}


def subject_for(job: dict, hint: str | None, profile: dict) -> str:
    name = profile["personal"]["full_name"]
    if hint and not re.search(r"\b(your|the) (name|position|role|job title)\b|\[|<", hint, re.I):
        return hint
    if hint:  # e.g. "the job title and your name"
        return f"{job['title']} - {name}"
    return f"Application: {job['title']} - {name}"


COVER_SYSTEM = ("You write concise, specific cover letters (180-260 words) for a job seeker. Use ONLY facts from the "
                "candidate profile. Never invent employers, dates, numbers, degrees, tools or achievements. Do not "
                "mention salary or notice period. No placeholders, no address block, no date line. Start with "
                "'Dear Hiring Manager,' (or the named contact if the job names one) and end with 'Kind regards,' and "
                "the candidate's full name.")


def _template_letter(profile: dict, job: dict, track: str) -> str:
    p = profile["personal"]
    summary = profile["summaries"].get(track) or profile["summaries"]["general"]
    company = job.get("company") or "your team"
    return (f"Dear Hiring Manager,\n\nI am writing to apply for the {job['title']} role at {company}. "
            f"{summary}\n\nI would welcome the chance to bring this experience to {company}. My tailored CV is attached, "
            f"and you can see more of my work at {p.get('portfolio') or p.get('github')}.\n\n"
            f"Thank you for your time and consideration.\n\nKind regards,\n{p['full_name']}\n{p['phone']} | {p['email']}")


def cover_letter(profile: dict, job: dict, track: str) -> tuple[str, str]:
    """(letter, source) where source is 'ai_draft' or 'template'."""
    if llm_client.available():
        facts = _profile_facts(profile)
        prompt = (f"Candidate profile:\n{facts}\nContact: {profile['personal']['phone']} | {profile['personal']['email']} | "
                  f"{profile['personal'].get('portfolio')}\n\nJob: {job['title']} at {job.get('company')}\n"
                  f"{(job.get('description') or '')[:5000]}\n\nWrite the cover letter.")
        try:
            text = llm_client.complete(prompt, COVER_SYSTEM)
            bad = _unverified_numbers(text, facts + " " + profile["personal"]["phone"] + " " + job["title"] + " " +
                                      (job.get("description") or "")[:5000])
            if text and not bad and "[" not in text:
                return text.strip(), "ai_draft"
            log.info("AI cover letter for %s had unverified details %s; using the plain template.", job["title"], bad)
        except Exception as exc:
            log.info("AI cover letter failed (%s); using the plain template.", exc)
    return _template_letter(profile, job, track), "template"


def _letter_docx(text: str, path: Path) -> Path:
    from docx import Document
    d = Document()
    for para in text.split("\n\n"):
        d.add_paragraph(para.strip())
    d.save(path)
    return path


def stage(conn, ws, job: dict, cv_id: int, cv_path: Path, track: str, found: dict, profile: dict) -> str:
    letter, source = cover_letter(profile, job, track)
    subject = subject_for(job, found.get("subject_hint"), profile)
    answers = [
        {"label": "To", "kind": "text", "value": found["to"], "source": "job post", "required": True, "filled": True,
         "needs_user": False, "consent": False, "note": "address taken from the job post"},
        {"label": "Subject", "kind": "text", "value": subject, "source": "job post" if found.get("subject_hint") else "profile",
         "required": True, "filled": True, "needs_user": False, "consent": False, "note": found.get("subject_hint") and "the post asked for this subject"},
        {"label": "Cover letter", "kind": "textarea", "value": letter, "source": "ai_draft" if source == "ai_draft" else "profile",
         "required": True, "filled": True, "needs_user": False, "consent": False, "note": "also attached as a Word file"},
        {"label": "CV", "kind": "file", "value": Path(cv_path).name, "source": "cv", "required": True, "filled": True,
         "needs_user": False, "consent": False, "note": None},
    ]
    conn.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, answers_json, missing_json, state, error, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (job["id"], cv_id, f"mailto:{found['to']}", json.dumps(answers), "[]",
                                              "READY_TO_SUBMIT", "Apply by email: the agent emails your CV and cover letter.",
                                              db.now_iso()))
    db.set_job_status(conn, job["id"], "READY_TO_SUBMIT", f"apply by email to {found['to']}")
    return "READY_TO_SUBMIT"


def send(ws, staged_id: int, overrides: dict | None = None, smtp_factory=None) -> dict:
    """Email the application. Refuses to send the same job twice."""
    overrides = {" ".join(k.lower().split()): v for k, v in (overrides or {}).items() if v not in (None, "")}
    with db.session(ws.db_path) as conn:
        row = conn.execute("SELECT s.*, j.title, j.company FROM staged_forms s JOIN jobs j ON j.id = s.job_id WHERE s.id = ?",
                           (staged_id,)).fetchone()
        if row is None or not (row["form_url"] or "").startswith("mailto:"):
            return {"ok": False, "error": "not an email application"}
        if row["state"] in ("SUBMITTED", "SENT_UNCONFIRMED"):
            return {"ok": False, "error": "already sent"}
        cv = conn.execute("SELECT docx_path FROM tailored_cvs WHERE id = ?", (row["cv_id"],)).fetchone()
    vals = {a["label"].lower(): a.get("value") for a in json.loads(row["answers_json"] or "[]")}
    vals.update({k: v for k, v in overrides.items()})
    to, subject, letter = vals.get("to"), vals.get("subject"), vals.get("cover letter") or ""
    if not (to and EMAIL_RE.fullmatch(to)):
        return {"ok": False, "error": f"bad address: {to}"}
    if re.search(r"\[[^\]]*\]", letter):
        return {"ok": False, "error": "the cover letter still has a placeholder in [brackets]"}
    with db.session(ws.db_path) as conn:
        dup = conn.execute("SELECT 1 FROM staged_forms s JOIN jobs j ON j.id = s.job_id WHERE s.id != ? AND s.form_url = ? "
                           "AND s.state IN ('SUBMITTED','SENT_UNCONFIRMED') AND lower(j.title) = lower(?)",
                           (staged_id, row["form_url"], row["title"])).fetchone()
        if dup:
            conn.execute("UPDATE staged_forms SET state = 'SKIPPED', error = ? WHERE id = ?",
                         ("Already emailed this application from another listing.", staged_id))
            db.set_job_status(conn, row["job_id"], "SKIPPED", "duplicate of an application already emailed")
            return {"ok": False, "error": "already emailed this job from another listing"}
    if not cv or not Path(cv["docx_path"]).exists():
        return {"ok": False, "error": "tailored CV file is missing"}
    addr, pw = os.environ.get("EMAIL_ADDRESS"), os.environ.get("EMAIL_APP_PASSWORD")
    if not (addr and pw):
        return {"ok": False, "error": "EMAIL_ADDRESS / EMAIL_APP_PASSWORD missing in .env"}

    from ..cv_tailor.single_template_tailor import load_profile
    name = load_profile()["personal"]["full_name"]
    safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    letter_path = _letter_docx(letter, ws.tailored_cvs / f"{row['job_id']}_cover_letter.docx")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = f"{name} <{addr}>", to, subject, make_msgid()
    msg.set_content(letter)
    for path, fname in ((Path(cv["docx_path"]), f"{safe}_CV.docx"), (letter_path, f"{safe}_Cover_Letter.docx")):
        msg.add_attachment(path.read_bytes(), maintype="application",
                           subtype="vnd.openxmlformats-officedocument.wordprocessingml.document", filename=fname)
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(host, port, timeout=60))
    try:
        with factory() as s:
            s.login(addr, pw.replace(" ", ""))
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error": f"could not send: {exc}"}
    with db.session(ws.db_path) as conn:
        conn.execute("UPDATE staged_forms SET state = 'SUBMITTED', submitted_at = ?, error = ? WHERE id = ?",
                     (db.now_iso(), f"Emailed to {to}", staged_id))
        db.set_job_status(conn, row["job_id"], "SUBMITTED", f"emailed CV and cover letter to {to}")
    log.info("Emailed application for %s @ %s to %s", row["title"], row["company"], to)
    return {"ok": True}
