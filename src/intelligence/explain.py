"""'Why this job': which of your skills the post asks for, what it asks that you don't show, and the scores."""
from __future__ import annotations

import re

from ..cv_tailor.single_template_tailor import _tokens
from .job_ranker import fit_score, low_applicant_score, recency_score

REQ_HEAD = re.compile(r"^\s*(requirements?|qualifications?|what you('|’)ll (need|bring)|what we('|’)re looking for|must[- ]haves?|"
                      r"key (skills|competencies)|minimum qualifications|you have|about you|skills( and experience)?)\b", re.I)
END_HEAD = re.compile(r"^\s*(benefits|what we offer|perks|about us|how to apply|why join|compensation|salary)\b", re.I)
STOP = {"experience", "years", "year", "strong", "ability", "knowledge", "work", "team", "skills", "excellent", "good", "including",
        "related", "field", "degree", "bachelor", "relevant", "least", "working", "understanding", "proven", "communication",
        "with", "and", "the", "for", "fluent", "plus", "using", "must", "have"}


def requirement_lines(text: str, limit: int = 14) -> list[str]:
    """The bullet points under 'Requirements' / 'Qualifications' (or requirement-like lines if there is no such heading)."""
    lines = [ln.strip(" •-*·\t") for ln in (text or "").splitlines()]
    out, inside = [], False
    for ln in lines:
        if not ln:
            continue
        if REQ_HEAD.match(ln) and len(ln) < 60:
            inside = True
            continue
        if inside and END_HEAD.match(ln) and len(ln) < 60:
            inside = False
            continue
        if inside and 12 <= len(ln) <= 220:
            out.append(ln)
    if not out:
        out = [ln for ln in lines if 12 <= len(ln) <= 220
               and re.search(r"\b(experience|knowledge|proficien|degree|familiar|ability|skills?)\b", ln, re.I)]
    return out[:limit]


def _profile_terms(profile: dict, track: str) -> set[str]:
    terms = set()
    for k in profile["tracks"].get(track, {}).get("keywords", []):
        terms |= _tokens(k)
    for g in profile["skills"].get(track, []) + profile["skills"].get("general", []):
        for item in g["items"]:
            terms |= _tokens(item)
    for r in profile.get("experience", []):
        for b in r.get("bullets", []):
            terms |= _tokens(b.get("text", ""))
    return {t for t in terms if t not in STOP and len(t) > 1}


def explain(job: dict, profile: dict, max_age: float = 24.0) -> dict:
    fit, track = fit_score(job, profile)
    text = f"{job.get('title', '')}\n{job.get('description') or ''}".lower()
    kws = profile["tracks"].get(track, {}).get("keywords", [])
    matched_kw = [k for k in kws if re.search(r"(?<![a-z])" + re.escape(k.lower()) + r"(?![a-z])", text)]
    mine = _profile_terms(profile, track)
    matched, missing = [], []
    for ln in requirement_lines(job.get("description") or ""):
        toks = {t for t in _tokens(ln) if t not in STOP and len(t) > 1}
        hit = sorted(toks & mine)
        (matched if hit else missing).append({"text": ln, "because": hit[:5]})
    reasons = []
    if matched_kw:
        reasons.append(f"Matches your {track.replace('_', ' ')} track: {', '.join(matched_kw[:6])}")
    if job.get("age_hours") is not None:
        reasons.append(f"Posted {job['age_hours']:.0f} hours ago")
    if job.get("applicant_count") is not None:
        reasons.append(f"{job['applicant_count']} applicants so far")
    if re.search(r"\bsenior\b|\bsr\.?\b|\blead\b", (job.get("title") or "").lower()):
        reasons.append("Senior title: a stretch for 4 years of experience, so its fit counts 20% less")
    return {"track": track, "fit": fit,
            "scores": {"fit": fit, "recency": round(recency_score(job.get("age_hours") if job.get("age_hours") is not None else max_age, max_age), 3),
                       "low_applicants": round(low_applicant_score(job.get("applicant_count")), 3), "priority": job.get("priority_score")},
            "keywords": matched_kw, "reasons": reasons, "requirements_met": matched, "requirements_missing": missing}
