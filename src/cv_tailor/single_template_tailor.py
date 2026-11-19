"""Builds a tailored CV for one job from the single master template.

How tailoring works (no facts are ever invented):
  1. Pick the track (ai_data, software, mechanical, general) that best matches
     the job text, unless one is given.
  2. Use that track's summary, skills and the bullets tagged for it.
  3. Reorder bullets inside each role, and skills inside each group, so the
     ones that share the most keywords with the job come first.
  4. Write everything into a copy of resumes/master_cv_template.docx using
     its named styles, so layout and typography never change.
  5. Check that every sentence in the output exists word for word in
     config/master_profile.json. If not, refuse to save.

Step 3 of the plan adds optional LLM help to choose emphasis; it will still
only select and reorder profile text, and step 5 still guards it.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "config" / "master_profile.json"
TEMPLATE_PATH = ROOT / "resumes" / "master_cv_template.docx"
TRACKS = ("ai_data", "software", "mechanical", "general")
STOPWORDS = set("""a an and are as at be by for from in into is it of on or our the their this to we with you your will
using across including such per via over under than also have has can all any new more most very within work role team""".split())


class FabricationError(RuntimeError):
    """Raised when the CV would contain text that is not in the profile."""


def load_profile(path: Path = PROFILE_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+#./-]*", text.lower())
    return {_stem(w.strip(".-/")) for w in words if len(w) > 1 and w not in STOPWORDS}


def _overlap(text: str, job_tokens: set[str]) -> int:
    return len(_tokens(text) & job_tokens)


def choose_track(profile: dict, job_text: str) -> str:
    """Pick the track whose keyword list best matches the job text."""
    low = job_text.lower()
    scores = {}
    for name in ("ai_data", "software", "mechanical"):
        kws = profile["tracks"][name]["keywords"]
        scores[name] = sum(1 for k in kws if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", low))
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


@dataclass
class TailoredCV:
    track: str
    headline: str
    summary: str
    experience: list[dict] = field(default_factory=list)
    education: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)


def tailor(profile: dict, job_text: str = "", track: str | None = None,
           max_bullets_per_role: int = 5) -> TailoredCV:
    track = track or choose_track(profile, job_text)
    if track not in TRACKS:
        raise ValueError(f"Unknown track {track!r}; use one of {TRACKS}")
    jt = _tokens(job_text)

    experience = []
    for role in profile["experience"]:
        if track not in role["tracks"]:
            continue
        bullets = [b["text"] for b in role["bullets"] if track in b["tracks"]]
        if jt:
            bullets = sorted(bullets, key=lambda t: -_overlap(t, jt))  # stable sort keeps CV order on ties
        experience.append({**{k: role[k] for k in ("title", "company", "location", "start", "end")},
                           "bullets": bullets[:max_bullets_per_role]})

    by_id = {e["id"]: e for e in profile["education"]}
    education = []
    for eid in profile["education_order"][track]:
        e = by_id[eid]
        education.append({**{k: e[k] for k in ("qualification", "institution", "location", "start", "end")},
                          "details": [d["text"] for d in e["details"] if track in d["tracks"]]})

    skills = []
    for g in profile["skills"][track]:
        items = list(g["items"])
        if jt:
            items = sorted(items, key=lambda t: -_overlap(t, jt))
        skills.append({"group": g["group"], "items": items})

    certs = [c["text"] for c in profile["certifications"] if track in c["tracks"]]

    profile_vocab = set()
    for blob in [profile["summaries"][track]] + [b for r in experience for b in r["bullets"]] + \
            [i for g in skills for i in g["items"]]:
        profile_vocab |= _tokens(blob)
    matched = sorted(jt & profile_vocab)

    return TailoredCV(track=track, headline=profile["tracks"][track]["headline"],
                      summary=profile["summaries"][track], experience=experience,
                      education=education, skills=skills, certifications=certs,
                      matched_keywords=matched)


def _allowed_texts(profile: dict) -> set[str]:
    p = profile["personal"]
    allowed = {p["full_name"].upper(), "PROFESSIONAL SUMMARY", "WORK EXPERIENCE", "EDUCATION",
               "SKILLS", "CERTIFICATIONS"}
    allowed |= {t["headline"] for t in profile["tracks"].values()}
    allowed |= set(profile["summaries"].values())
    for r in profile["experience"]:
        allowed.add(f"{r['title']}\t{r['start']} – {r['end']}")
        allowed.add(f"{r['company']}, {r['location']}")
        allowed |= {f"•\t{b['text']}" for b in r["bullets"]}
    for e in profile["education"]:
        allowed.add(f"{e['qualification']}\t{e['start']} – {e['end']}")
        allowed.add(f"{e['institution']}, {e['location']}")
        allowed |= {f"•\t{d['text']}" for d in e["details"]}
    allowed |= {f"•\t{c['text']}" for c in profile["certifications"]}
    return allowed


def _skill_line_ok(profile: dict, text: str) -> bool:
    m = re.match(r"^•\t(.+?): (.+)$", text)
    if not m:
        return False
    group, items = m.group(1), [i.strip() for i in m.group(2).split(", ")]
    for track_groups in profile["skills"].values():
        for g in track_groups:
            if g["group"] == group:
                joined = ", ".join(g["items"])
                if all(i in g["items"] or i in joined for i in items):
                    return True
    return False


def verify_no_fabrication(profile: dict, doc: Document) -> None:
    allowed = _allowed_texts(profile)
    p = profile["personal"]
    contact_bits = {v for v in (p["location"], p["phone"], p["email"], p["linkedin"], p["github"], p.get("portfolio")) if v}
    bad = []
    for para in doc.paragraphs:
        t = para.text
        if not t.strip() or t in allowed:
            continue
        if para.style.name == "CV Contact" and all(part.strip() in contact_bits for part in t.split("|")):
            continue
        if para.style.name == "CV Bullet" and _skill_line_ok(profile, t):
            continue
        bad.append(t)
    if bad:
        raise FabricationError("Text not found in master_profile.json:\n  " + "\n  ".join(bad))


def render(profile: dict, cv: TailoredCV, out_path: Path, template: Path = TEMPLATE_PATH) -> Path:
    doc = Document(str(template))
    body = doc.element.body
    sect = body.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}sectPr")
    sect = copy.deepcopy(sect)
    for child in list(body):
        body.remove(child)
    body.append(sect)

    def add(text, style):
        doc.add_paragraph(text, style=style)

    p = profile["personal"]
    add(p["full_name"].upper(), "CV Name")
    add(cv.headline, "CV Headline")
    add(" | ".join([p["location"], p["phone"], p["email"]]), "CV Contact")
    add(" | ".join(v for v in (p["linkedin"], p["github"], p.get("portfolio")) if v), "CV Contact")

    add("PROFESSIONAL SUMMARY", "CV Heading")
    add(cv.summary, "CV Body")

    add("WORK EXPERIENCE", "CV Heading")
    for r in cv.experience:
        add(f"{r['title']}\t{r['start']} – {r['end']}", "CV Role")
        add(f"{r['company']}, {r['location']}", "CV Company")
        for b in r["bullets"]:
            add(f"•\t{b}", "CV Bullet")

    add("EDUCATION", "CV Heading")
    for e in cv.education:
        add(f"{e['qualification']}\t{e['start']} – {e['end']}", "CV Role")
        add(f"{e['institution']}, {e['location']}", "CV Company")
        for d in e["details"]:
            add(f"•\t{d}", "CV Bullet")

    add("SKILLS", "CV Heading")
    for g in cv.skills:
        para = doc.add_paragraph(style="CV Bullet")
        para.add_run("•\t")
        para.add_run(f"{g['group']}: ").bold = True
        para.add_run(", ".join(g["items"]))

    add("CERTIFICATIONS", "CV Heading")
    for c in cv.certifications:
        add(f"•\t{c}", "CV Bullet")

    verify_no_fabrication(profile, doc)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.core_properties.author = p["full_name"]
    doc.core_properties.title = f"{p['full_name']} CV"
    doc.save(str(out_path))
    return out_path


def build_cv(job_text: str, out_path: Path, track: str | None = None,
             profile: dict | None = None) -> tuple[Path, TailoredCV]:
    profile = profile or load_profile()
    if not TEMPLATE_PATH.exists():
        from resumes.build_master_template import build
        build(TEMPLATE_PATH)
    cv = tailor(profile, job_text, track)
    return render(profile, cv, out_path), cv
