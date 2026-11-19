"""Answers application-form questions using only verified facts from master_profile.json.

Order of trust:
  1. Direct profile facts (name, email, phone, links, location, education...).
  2. screening_answers you filled in. A null answer is never guessed: the
     question is reported as NEEDS_USER_INPUT.
  3. Demographic (EEO) questions: pick a "decline to answer" option if the form
     offers one, otherwise ask you.
  4. Consent / terms checkboxes are never ticked by the agent. They are shown
     on the review page for you to tick yourself before Submit.
  5. Open written questions ("Why do you want to join?") get a short draft from
     the free LLM, built only from your profile and the job text, and marked
     as an AI draft for you to read before submitting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import llm_client

YES = ("yes", "true")
NO = ("no", "false")


@dataclass
class Question:
    label: str
    kind: str = "text"            # text | textarea | select | radio | checkbox | file | email | tel | url | number
    options: list[str] = field(default_factory=list)
    required: bool = False


@dataclass
class Answer:
    value: object = None          # str, bool, or a path for file inputs
    source: str | None = None     # profile | screening | eeo | cv | ai_draft
    needs_user: bool = False
    consent: bool = False         # a terms/consent box the user must tick on the review page
    note: str | None = None


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[*:∗]+", " ", s or "").lower().split())


def pick_option(options: list[str], wanted) -> str | None:
    """Return the option that matches the wanted value (bool or text), or None."""
    if not options:
        return None
    if isinstance(wanted, bool):
        keys = YES if wanted else NO
        for o in options:
            if _norm(o).split(" ")[0].strip(",.") in keys:
                return o
        return None
    w = _norm(str(wanted))
    for o in options:
        if _norm(o) == w:
            return o
    for o in options:
        if w and (w in _norm(o) or _norm(o) in w):
            return o
    return None


DECLINE = re.compile(r"decline|prefer not|don.?t wish|do not wish|not to (say|answer|disclose)|i don.?t want", re.I)
EEO = re.compile(r"\b(gender|sex\b|race|racial|ethnic|hispanic|latino|veteran|disabilit|sexual orientation|transgender|pronoun)", re.I)
CONSENT = re.compile(r"\b(i agree|i consent|consent|privacy (policy|notice)|terms|acknowledge|i certify|i confirm|i understand|accurate and complete|data processing|gdpr)", re.I)
OPEN_TEXT = re.compile(r"\b(why|tell us|describe|explain|what interests|motivat|cover letter|additional information|anything else|about yourself|summary|how would you|what makes you)", re.I)


class AnswerEngine:
    def __init__(self, profile: dict, use_llm: bool = True):
        self.p = profile
        self.s = profile.get("screening_answers", {})
        self.use_llm = use_llm and llm_client.available()
        exp = profile["experience"][0] if profile.get("experience") else {}
        per = profile["personal"]
        # (pattern, value-getter, source). First match wins, so specific patterns come first.
        self.rules = [
            (r"\b(first|given) name\b|\bfirst\b$", lambda: per["first_name"], "profile"),
            (r"\b(last|family|sur) ?name\b", lambda: per["last_name"], "profile"),
            (r"\bpreferred (first )?name\b", lambda: per["first_name"], "profile"),
            (r"^(your )?(full |legal )?name$|\bfull name\b|\blegal name\b|^name\b", lambda: per["full_name"], "profile"),
            (r"\be-?mail\b", lambda: per["email"], "profile"),
            (r"\b(phone|mobile|telephone|cell)\b", lambda: per["phone"], "profile"),
            (r"linkedin", lambda: per.get("linkedin"), "profile"),
            (r"github", lambda: per.get("github"), "profile"),
            (r"\b(portfolio|personal (web)?site|website|other link|url)\b", lambda: per.get("portfolio") or per.get("github"), "profile"),
            (r"\b(current|most recent) (company|employer)\b|\borganization\b|\bcompany\b", lambda: exp.get("company", "").replace("™", ""), "profile"),
            (r"\b(current|most recent) (job )?title\b", lambda: exp.get("title"), "profile"),
            (r"\b(city|location|where are you (located|based)|current location|address)\b", lambda: per.get("location"), "profile"),
            (r"\bcountry\b", lambda: per.get("country"), "profile"),
            (r"\b(highest|level of) (education|degree)\b|\bdegree\b", lambda: self.s.get("highest_education"), "screening"),
            (r"\b(field|area) of study\b|\bmajor\b|\bdiscipline\b", lambda: self.s.get("degree_field"), "screening"),
            (r"\b(school|university|college)\b", lambda: self.p["education"][0]["institution"], "profile"),
            (r"years? of (professional |relevant |work )?experience|how many years", lambda: self.s.get("years_of_experience_total"), "screening"),
            (r"\b(salary|compensation|pay|rate)\b.*\b(hour|hourly|usd|\$)|\bhourly\b", lambda: self.s.get("expected_hourly_rate_usd"), "screening"),
            (r"\b(salary|compensation|pay expectation|expected pay)\b", lambda: self.s.get("expected_salary_monthly_kes"), "screening"),
            (r"\bnotice period\b", lambda: self.s.get("notice_period"), "screening"),
            (r"\b(start date|when can you start|earliest)\b", lambda: self.s.get("earliest_start_date"), "screening"),
            (r"\b(sponsor|sponsorship|visa)\b", lambda: self.s.get("requires_visa_sponsorship_outside_kenya"), "screening"),
            (r"\b(authori[sz]ed|eligible|legally (able|permitted)|right to work|work permit)\b", lambda: self.s.get("authorized_to_work_in_kenya"), "screening"),
            (r"\brelocat", lambda: self.s.get("willing_to_relocate"), "screening"),
            (r"\b(remote|work remotely)\b", lambda: self.s.get("willing_to_work_remotely"), "screening"),
            (r"\blanguages?\b", lambda: self.s.get("languages"), "screening"),
            (r"driver.?s licen[cs]e", lambda: self.s.get("has_drivers_license"), "screening"),
            (r"how did you (hear|find|learn)|\bsource\b|\breferr", lambda: self.s.get("how_did_you_hear"), "screening"),
        ]

    def answer(self, q: Question, job: dict | None = None, cv_path: str | None = None) -> Answer:
        label = _norm(q.label)
        if q.kind == "file":
            if re.search(r"resume|\bcv\b|curriculum", label) or (q.required and "cover" not in label):
                return Answer(cv_path, "cv") if cv_path else Answer(needs_user=q.required, note="no CV built")
            return Answer(needs_user=q.required, note="file upload left for you")
        if CONSENT.search(label) and q.kind in ("checkbox", "radio", "select"):
            return Answer(consent=True, needs_user=q.required, note="tick this yourself on the review page")
        if EEO.search(label):
            opt = next((o for o in q.options if DECLINE.search(o)), None)
            if opt:
                return Answer(opt, "eeo")
            if q.kind in ("text", "textarea") and not q.required:
                return Answer(None, "eeo")
            return Answer(needs_user=q.required, note="demographic question with no 'decline' option")
        if q.kind in ("text", "textarea") and OPEN_TEXT.search(label):
            return self._draft(q, job)
        for pattern, getter, source in self.rules:
            if re.search(pattern, label):
                val = getter()
                if val is None or val == "":
                    return Answer(needs_user=q.required, note=f"fill '{q.label}' in screening_answers")
                return self._fit(q, val, source)
        if q.kind in ("textarea", "text") and (OPEN_TEXT.search(label) or q.kind == "textarea"):
            return self._draft(q, job)
        return Answer(needs_user=q.required, note="no verified answer in your profile")

    def _fit(self, q: Question, val, source: str) -> Answer:
        if q.kind in ("select", "radio"):
            opt = pick_option(q.options, val)
            if opt is None and isinstance(val, (int, float)) and not isinstance(val, bool):
                opt = _pick_number_range(q.options, val)
            return Answer(opt, source) if opt else Answer(needs_user=q.required, note=f"none of the options match '{val}'")
        if q.kind == "checkbox":
            return Answer(bool(val), source) if isinstance(val, bool) else Answer(needs_user=q.required)
        if isinstance(val, bool):
            return Answer("Yes" if val else "No", source)
        return Answer(str(val), source)

    def _draft(self, q: Question, job: dict | None) -> Answer:
        if not self.use_llm:
            return Answer(needs_user=q.required, note="written answer needed (no LLM key)")
        facts = _profile_facts(self.p)
        job_text = ((job or {}).get("title", "") + " at " + ((job or {}).get("company") or "") + "\n" +
                    ((job or {}).get("description") or "")[:3000])
        prompt = (f"Application question: {q.label}\n\nJob:\n{job_text}\n\nCandidate facts (the ONLY facts you may use):\n{facts}\n\n"
                  "Write the candidate's answer in first person, 60 to 120 words, plain text, no greeting or sign-off. "
                  "Use only the facts above. Do not invent employers, dates, numbers, degrees or skills. "
                  "If the facts don't support an answer, reply exactly: NEEDS_USER_INPUT")
        try:
            text = llm_client.complete(prompt, system="You write truthful, concise job application answers.")
        except llm_client.LLMUnavailable as exc:
            return Answer(needs_user=q.required, note=f"LLM unavailable ({exc})")
        if "NEEDS_USER_INPUT" in text or not text.strip():
            return Answer(needs_user=q.required, note="the AI could not answer this from your profile")
        bad = _unverified_numbers(text, facts)
        if bad:
            return Answer(needs_user=q.required, note=f"AI draft rejected: numbers not in your profile ({', '.join(bad)})")
        return Answer(text.strip(), "ai_draft", note="AI draft: read before submitting")


def _pick_number_range(options: list[str], n: float) -> str | None:
    for o in options:
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
        if len(nums) >= 2 and nums[0] <= n <= nums[1]:
            return o
        if len(nums) == 1 and re.search(r"\+|more|over|above", o, re.I) and n >= nums[0]:
            return o
        if len(nums) == 1 and re.search(r"less|under|below|fewer", o, re.I) and n < nums[0]:
            return o
        if len(nums) == 1 and nums[0] == n:
            return o
    return None


def _profile_facts(p: dict) -> str:
    lines = [f"Name: {p['personal']['full_name']}, based in {p['personal']['location']}."]
    lines.append("Summary: " + p["summaries"]["general"])
    for r in p["experience"]:
        lines.append(f"{r['title']}, {r['company']} ({r['start']} – {r['end']}): " + " ".join(b["text"] for b in r["bullets"]))
    for e in p["education"]:
        lines.append(f"{e['qualification']}, {e['institution']} ({e['start']} – {e['end']}).")
    lines.append("Certifications: " + "; ".join(c["text"] for c in p["certifications"]))
    return "\n".join(lines)


def _unverified_numbers(text: str, facts: str) -> list[str]:
    allowed = set(re.findall(r"\d+(?:[.,]\d+)?", facts)) | {"1", "2", "3", "one", "two"}
    return sorted({n for n in re.findall(r"\d+(?:[.,]\d+)?", text) if n not in allowed})
