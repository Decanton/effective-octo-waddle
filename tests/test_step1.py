import json
import sqlite3
from pathlib import Path

import pytest
from docx import Document

from src import database as db
from src.cv_tailor import single_template_tailor as t
from src.workspace import SUBFOLDERS, ensure_workspace


def test_workspace_created(tmp_path):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    for s in SUBFOLDERS:
        assert (ws.root / s).is_dir()
    ensure_workspace(ws.root)  # idempotent


def test_db_init_and_upsert(tmp_path):
    p = tmp_path / "x.sqlite3"
    db.init_db(p)
    with db.session(p) as c:
        jid = db.upsert_job(c, {"url": "https://a/1", "source": "test", "title": "Backend Engineer"})
        assert db.upsert_job(c, {"url": "https://a/1", "source": "test", "title": "Backend Engineer", "fit_score": 0.5}) == jid
        db.set_job_status(c, jid, "QUALIFIED")
        assert db.count_by_status(c) == {"QUALIFIED": 1}
        with pytest.raises(ValueError):
            db.set_job_status(c, jid, "BOGUS")


@pytest.mark.parametrize("text,track", [
    ("Seeking an LLM evaluator for RLHF and prompt engineering annotation work", "ai_data"),
    ("Backend Python Django developer, REST APIs and PostgreSQL", "software"),
    ("Mechanical design engineer with SolidWorks and FEA", "mechanical"),
    ("Office administrator", "general"),
])
def test_choose_track(text, track):
    assert t.choose_track(t.load_profile(), text) == track


@pytest.mark.parametrize("track", t.TRACKS)
def test_render_each_track(tmp_path, track):
    path, cv = t.build_cv("", tmp_path / f"{track}.docx", track=track)
    doc = Document(str(path))
    styles = {p.style.name for p in doc.paragraphs}
    assert {"CV Name", "CV Heading", "CV Role", "CV Bullet"} <= styles
    assert doc.paragraphs[0].text == "DICKENS OWUOR"


def test_keyword_reordering_puts_relevant_bullet_first(tmp_path):
    _, cv = t.build_cv("Oracle PL/SQL performance tuning EXPLAIN PLAN indexes", tmp_path / "o.docx", track="software")
    radia = next(r for r in cv.experience if r["company"].startswith("Radia"))
    top = radia["bullets"][:3]
    assert any("EXPLAIN PLAN" in b for b in top)
    assert not any(b.startswith("Surfaced actionable") for b in top)


def test_fabrication_guard_blocks_invented_text(tmp_path):
    profile = t.load_profile()
    cv = t.tailor(profile, "", "software")
    cv.experience[0]["bullets"][0] = "Led a team of 50 engineers at Google."
    with pytest.raises(t.FabricationError):
        t.render(profile, cv, tmp_path / "bad.docx")
