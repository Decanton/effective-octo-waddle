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


def _yes_no_options(options: list[str]) -> bool:
    norm = {_norm(o).split(" ")[0].strip(",.") for o in options or []}
    return bool(options) and len(options) <= 4 and bool(norm & set(YES)) and bool(norm & set(NO))


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
            (r"\b(street|address line ?1|address 1|street address)\b", lambda: per.get("address_line"), "profile"),
            (r"\b(state|province|county|region)\b", lambda: per.get("county"), "profile"),
            (r"^(?!.*(authori|sponsor|visa|eligib|permit|legally|right to work|relocat|willing)).*\bcity\b", lambda: per.get("city"), "profile"),
            (r"\b(full )?(home |mailing |residential )?address\b", lambda: per.get("full_address") or per.get("location"), "profile"),
            (r"^(?!.*(authori|sponsor|visa|eligib|permit|legally|right to work|relocat|willing)).*\b(location|where are you (located|based)|current location|where do you (live|reside))\b", lambda: per.get("location"), "profile"),
            (r"^(?!.*(authori|sponsor|visa|eligib|permit|legally|right to work|relocat|willing)).*\bcountry\b", lambda: per.get("country"), "profile"),
            (r"\b(highest|level of) (education|degree)\b|\bdegree\b", lambda: self.s.get("highest_education"), "screening"),
            (r"\b(field|area) of study\b|\bmajor\b|\bdiscipline\b", lambda: self.s.get("degree_field"), "screening"),
            (r"\b(school|university|college)\b", lambda: self.p["education"][0]["institution"], "profile"),
            (r"years? of (professional |relevant |work )?experience|how many years", lambda: self.s.get("years_of_experience_total"), "screening"),
            (r"\b(salary|compensation|pay|rate|remuneration|ctc)\b.*\b(hour|hourly)\b|\bhourly\b|\bper hour\b", lambda: "__hourly__", "screening"),
            (r"\b(salary|compensation|pay expectations?|expected pay|remuneration|ctc|desired pay)\b", lambda: "__salary__", "screening"),
            (r"\bnotice( period)?\b|how (much|long).{0,20}notice", lambda: self.s.get("notice_period"), "screening"),
            (r"\b(start date|when can you start|earliest|available to start|availability to start|date available|join)\b",
             lambda: self.s.get("earliest_start_date"), "screening"),
            (r"\b(sponsor|sponsorship|visa)\b", lambda: "__sponsor__", "screening"),
            (r"\b(authori[sz](ed|ation)|eligible|eligibility|legally (able|permitted|entitled)|right to work|work permit|permission to work)\b",
             lambda: "__authorized__", "screening"),
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
            mine = self._demographic(q, label)
            if mine is not None:
                return mine
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
                if val in ("__hourly__", "__salary__"):
                    return self._salary(q, label, job, hourly=val == "__hourly__")
                if val in ("__sponsor__", "__authorized__"):
                    ok_here = self.works_without_visa(label, job)
                    if ok_here is None:
                        return Answer(needs_user=q.required, note="work-authorization question the agent couldn't place")
                    val = (not ok_here) if val == "__sponsor__" else ok_here
                    if not isinstance(val, bool) or self.s.get("authorized_to_work_in_kenya") is None:
                        return Answer(needs_user=q.required, note=f"fill work authorization in screening_answers")
                    return self._fit(q, val, source)
                if source == "screening" and re.search(r"notice|start", label) and q.kind in ("select", "radio"):
                    opt = self._notice_option(q.options)
                    if opt:
                        return Answer(opt, source)
                if source == "screening" and re.search(r"\bdate\b", label) and re.search(r"start|available|join", label) \
                        and q.kind in ("text", "date"):
                    from datetime import date, timedelta
                    days = int(self.s.get("notice_period_days") or 7)
                    return Answer((date.today() + timedelta(days=days)).isoformat(), source)
                if val is None or val == "":
                    return Answer(needs_user=q.required, note=f"fill '{q.label}' in screening_answers")
                return self._fit(q, val, source)
        yn = self.yes_no_default(label)
        if yn is not None and (q.kind in ("radio", "checkbox") or _yes_no_options(q.options)):
            return self._fit(q, yn, "standard")
        if q.kind in ("textarea", "text") and (OPEN_TEXT.search(label) or q.kind == "textarea"):
            return self._draft(q, job)
        return Answer(needs_user=q.required, note="no verified answer in your profile")

    # Standard yes/no questions with one honest answer for Kelly (2026-11-19: "fill every field").
    YES_NO = [
        (r"\b(18|eighteen)\b.{0,20}(years|older|age)|\bof (legal|working) age\b|\bover (the age of )?18\b", True),
        (r"\b(non-?compete|non-?solicit|post-?employment restrictions?|restrictive covenants?)\b", False),
        (r"\b(previously|ever|have you)\b.{0,30}\b(work(ed)?|employed|been employed)\b.{0,20}\b(at|by|for)\b", False),
        (r"\b(relatives?|family members?|related to)\b.{0,40}\b(employ|work)", False),
        (r"\b(convicted|felony|criminal (record|offen[cs]e))\b", False),
        (r"\b(require|need)\b.{0,20}\baccommodation", False),
        (r"\bbackground (check|screening)\b|\breference checks?\b|\bdrug (test|screen)", True),
        (r"\b(willing|able|comfortable|happy|open|available|agree) to\b|\bcan you\b|\bare you (able|willing|comfortable)\b", True),
        (r"\bwithout (the use of |using )?(ai|chatgpt|llm)", True),
        (r"\b(fluent|proficient)\b.{0,15}\benglish\b|\benglish\b.{0,25}\b(fluent|proficien|native|professional)", True),
    ]

    def yes_no_default(self, label: str) -> bool | None:
        for rx, val in self.YES_NO:
            if re.search(rx, label, re.I):
                return val
        return None

    def _demographic(self, q: Question, label: str) -> Answer | None:
        """Voluntary EEO questions answered from master_profile 'demographics' (Kelly, 2026-11-19)."""
        d = self.p.get("demographics") or {}
        opts = q.options or []
        def first(rx):
            return next((o for o in opts if re.search(rx, o, re.I) and not DECLINE.search(o)), None)
        if re.search(r"veteran", label) and d.get("protected_veteran") is False:
            o = first(r"\bnot\b.*veteran|\bno\b|i am not") if opts else "I am not a protected veteran"
        elif re.search(r"disabilit", label) and d.get("disability") is False:
            o = first(r"\bno\b|do not have|don.?t have|not have") if opts else "No, I do not have a disability"
        elif re.search(r"hispanic|latino", label) and d.get("hispanic_or_latino") is False:
            o = first(r"^\s*no\b|not hispanic|non-hispanic") if opts else "No"
        elif re.search(r"\brace\b|racial|ethnic", label) and d.get("race"):
            o = first(r"black|african") if opts else d["race"]
        elif re.search(r"gender|\bsex\b", label) and d.get("gender"):
            o = pick_option(opts, d["gender"]) if opts else d["gender"]
        else:
            return None
        return Answer(o, "eeo") if o else None

    KENYA = re.compile(r"\b(kenya|kenyan|nairobi|mombasa|kisumu|nakuru|eldoret|east africa)\b", re.I)
    FOREIGN = re.compile(r"\b(u\.?s\.?a?|united states|america|canada|u\.?k\.?|united kingdom|britain|england|ireland|"
                         r"europe|european union|\beu\b|eea|germany|france|netherlands|spain|portugal|poland|italy|sweden|"
                         r"switzerland|australia|new zealand|india|singapore|uae|dubai|saudi|qatar|south africa|nigeria|"
                         r"ghana|egypt|israel|japan|brazil|mexico|new york|san francisco|seattle|boston|austin|chicago|los angeles|"
                         r"london|berlin|amsterdam|paris|dublin|toronto|vancouver|sydney|bangalore|bengaluru)\b", re.I)
    HERE = re.compile(r"countr(y|ies) (where|in which) you (live|reside|are (based|located))|your (current )?(country|location)"
                      r"|where you (live|reside|are based)|country of residence", re.I)

    def works_without_visa(self, label: str, job: dict | None) -> bool | None:
        """True when the question is about Kenya (or remote work from Kenya), False for another country."""
        if self.HERE.search(label) or self.KENYA.search(label):
            return True
        if self.FOREIGN.search(label):
            return False
        loc = f"{(job or {}).get('location') or ''}"
        remote = bool((job or {}).get("is_remote")) or re.search(r"\bremote|anywhere|worldwide\b", loc, re.I)
        if self.KENYA.search(loc):
            return True
        if re.search(r",\s*[A-Z]{2}\b", loc) and not self.KENYA.search(loc):
            return False  # "New York, NY"-style US places
        if self.FOREIGN.search(loc) and not remote:
            return False
        if remote and not self.FOREIGN.search(loc):
            return True  # remote work done from Kenya
        if self.FOREIGN.search(loc):
            return False  # remote but tied to a foreign country, e.g. "Remote - US"
        return None

    def _salary(self, q: Question, label: str, job: dict | None, hourly: bool) -> Answer:
        """USD 20/hour (AI training), USD 800/month in Kenya, USD 2,500/month abroad."""
        s = self.s
        if hourly or (job or {}).get("track") == "ai_data" and re.search(r"\bhour", label):
            usd, unit = s.get("expected_hourly_rate_usd"), "per hour"
        else:
            in_kenya = self.works_without_visa(label, job) and self.KENYA.search(f"{label} {(job or {}).get('location') or ''}")
            usd = s.get("expected_salary_monthly_usd_kenya") if in_kenya else s.get("expected_salary_monthly_usd_abroad")
            unit = "per month"
            if re.search(r"\b(annual|annually|per year|yearly|a year|/ ?year|p\.?a\.?)\b", label):
                usd, unit = (usd * 12 if usd else None), "per year"
        if not usd:
            return Answer(needs_user=q.required, note="fill your expected pay in screening_answers")
        kes = bool(re.search(r"\b(kes|ksh|kshs|shillings?)\b", label))
        amount = round(usd * float(s.get("usd_to_kes") or 129), -3) if kes else usd
        if q.kind in ("select", "radio"):
            opt = _pick_number_range(q.options, amount)
            return Answer(opt, "screening") if opt else Answer(needs_user=q.required, note=f"no option fits {amount}")
        if q.kind == "number":
            return Answer(str(int(amount)), "screening")
        return Answer(f"{'KES' if kes else 'USD'} {int(amount):,} {unit}", "screening")

    NOTICE_PREFS = [r"\b(1|one) ?week|\b7 days|\bwithin a week", r"\bimmediate|right away|asap|\bnow\b|no notice|0 ?(days|weeks)",
                    r"less than (2|two) weeks|under (2|two) weeks|up to (2|two) weeks|(1|one) ?- ?(2|two) weeks|0 ?- ?(2|two) weeks|"
                    r"within (2|two) weeks|(2|two) weeks or less|less than (a|1|one) month|under (a|1|one) month"]

    def _notice_option(self, options: list[str]) -> str | None:
        for pref in self.NOTICE_PREFS:
            for o in options:
                if re.search(pref, o, re.I):
                    return o
        return None

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
        nums = [float(x.replace(",", "")) * (1000 if k.lower() == "k" else 1)
                for x, k in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*([kK]?)", o)]
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
