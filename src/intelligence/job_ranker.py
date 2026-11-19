"""Applies the 24-hour rule and knockouts, then scores fit and low-applicant priority.

priority = w_fit * fit + w_recency * recency + w_low * low_applicant_score

  fit           0..1  how well the job matches your track keywords and profile skills
  recency       0..1  1.0 when just posted, 0.0 at the 24-hour limit
  low_applicant 0..1  verified low counts score high; an unknown count is
                      neutral (0.5) so it is neither boosted nor punished
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..cv_tailor.single_template_tailor import _tokens, choose_track
from ..discovery.freshness import is_fresh


@dataclass
class Verdict:
    keep: bool
    status: str
    reason: str | None = None


def low_applicant_score(n: int | None, threshold: int = 25) -> float:
    if n is None:
        return 0.5
    if n <= 10:
        return 1.0
    if n <= threshold:
        return 0.85
    if n <= 50:
        return 0.6
    if n <= 100:
        return 0.4
    if n <= 200:
        return 0.2
    return 0.05


def recency_score(age_hours: float, max_age: float = 24.0) -> float:
    return max(0.0, min(1.0, 1 - age_hours / max_age))


def _contains_any(text: str, phrases) -> str | None:
    low = f" {text.lower()} "
    for p in phrases or []:
        if p.lower() in low:
            return p
    return None


def max_age_for(job: dict, search_cfg: dict) -> float:
    """The age limit for this job's source (company boards may allow longer; see search_queries.yaml)."""
    kind = (job.get("source") or "").split(":")[0]
    per = search_cfg.get("max_age_hours_by_source") or {}
    return float(per.get(kind, search_cfg.get("max_age_hours", 48)))


def check_job(job: dict, search_cfg: dict, platform_cfg: dict | None = None) -> Verdict:
    max_age = max_age_for(job, search_cfg)
    if job.get("age_hours") is None:
        return Verdict(False, "REJECTED_STALE", "no reliable posting date")
    if not is_fresh(job["age_hours"], max_age):
        return Verdict(False, "REJECTED_STALE", f"posted {job['age_hours']:.0f}h ago")
    hit = _contains_any(job["title"], search_cfg.get("title_exclude"))
    if hit:
        return Verdict(False, "REJECTED_KNOCKOUT", f"title contains '{hit.strip()}'")
    hit = _contains_any(job.get("description") or "", search_cfg.get("description_knockouts"))
    if hit:
        return Verdict(False, "REJECTED_KNOCKOUT", f"description says '{hit}'")
    loc = (job.get("location") or "").lower()
    if job.get("is_remote"):
        hit = _contains_any(f"{loc} {job['title']} {(job.get('description') or '')[:600]}",
                            search_cfg.get("remote_region_exclude"))
        if hit:
            return Verdict(False, "REJECTED_KNOCKOUT", f"remote but '{hit}'")
        place = re.sub(r"\b(remote|hybrid|work from home|wfh|fully|100%)\b|[()\-–,;/|]", " ", loc).strip()
        if place and not any(w in loc for w in search_cfg.get("remote_locations_ok", [])):
            return Verdict(False, "REJECTED_KNOCKOUT", f"remote but limited to '{job.get('location')}'")
    else:
        ok = list(search_cfg.get("onsite_locations_ok", [])) + list((platform_cfg or {}).get("locations_ok", []))
        if not any(w in loc for w in ok):
            return Verdict(False, "REJECTED_KNOCKOUT", f"onsite in '{job.get('location') or 'unknown'}'")
    return Verdict(True, "QUALIFIED")


def fit_score(job: dict, profile: dict) -> tuple[float, str]:
    text = f"{job['title']} {job['title']} {job.get('description') or ''}"
    track = choose_track(profile, text)
    if job.get("track") and track == "general":
        track = job["track"]
    low = text.lower()
    kws = profile["tracks"].get(track, {}).get("keywords", [])
    title_low = job["title"].lower()
    kw_hits = sum(1 for k in kws if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", low))
    title_hits = sum(1 for k in kws if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", title_low))
    skill_tokens = set()
    for g in profile["skills"].get(track, []):
        for item in g["items"]:
            skill_tokens |= _tokens(item)
    job_tokens = _tokens(text)
    skill_overlap = len(job_tokens & skill_tokens) / max(8, min(len(skill_tokens), 25))
    if len((job.get("description") or "").strip()) < 200:  # only a title to go on (e.g. a LinkedIn card): judge the title
        score = 0.65 * min(1.0, title_hits / 1.5) + 0.35 * min(1.0, kw_hits / 3)
    else:
        score = 0.45 * min(1.0, kw_hits / 6) + 0.30 * min(1.0, title_hits / 2) + 0.25 * min(1.0, skill_overlap)
    if re.search(r"\bsenior\b|\bsr\.?\b|\blead\b", title_low):
        score *= 0.8  # four years of experience: senior roles are a stretch, not a knockout
    return round(score, 4), track


def rank(jobs: list[dict], profile: dict, search_cfg: dict, platform_cfg: dict | None = None) -> list[dict]:
    """Return every job annotated with status, reason, track and scores; qualified ones first by priority."""
    w = profile.get("ranking_weights", {})
    wf, wr, wl = w.get("fit", 0.55), w.get("recency", 0.2), w.get("low_applicants", 0.25)
    thr = int(w.get("low_applicant_threshold", 25))
    max_age = float(search_cfg.get("max_age_hours", w.get("max_age_hours", 48)))
    out, seen = [], set()
    for j in jobs:
        key = (j["title"].lower().strip(), (j.get("company") or "").lower().strip())
        v = check_job(j, search_cfg, platform_cfg)
        if v.keep and key in seen:
            v = Verdict(False, "SKIPPED", "duplicate of another listing")
        j = dict(j, status=v.status, status_reason=v.reason)
        if v.keep:
            seen.add(key)
            fit, track = fit_score(j, profile)
            pri = wf * fit + wr * recency_score(j["age_hours"], max_age_for(j, search_cfg)) + wl * low_applicant_score(j.get("applicant_count"), thr)
            pri += float((w.get("track_bonus") or {}).get(track, 0.0))  # e.g. AI training roles first
            j.update(track=track, fit_score=fit, priority_score=round(pri, 4))
        out.append(j)
    min_fit = float(w.get("min_fit", 0.30))
    for j in out:
        if j["status"] == "QUALIFIED" and j["fit_score"] < min_fit:
            j.update(status="REJECTED_KNOCKOUT", status_reason=f"weak fit ({j['fit_score']:.2f})")
    return sorted(out, key=lambda j: (j["status"] != "QUALIFIED", -(j.get("priority_score") or 0)))
