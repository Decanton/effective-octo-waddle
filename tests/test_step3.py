import json
import os
from pathlib import Path

import pytest

from src import database as db
from src.cv_tailor.single_template_tailor import load_profile
from src.intelligence.answer_engine import AnswerEngine, Question, pick_option
from src.workspace import ensure_workspace

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def engine():
    return AnswerEngine(load_profile(), use_llm=False)


def test_profile_answers(engine):
    p = load_profile()["personal"]
    assert engine.answer(Question("First Name", required=True)).value == p["first_name"]
    assert engine.answer(Question("Last name")).value == p["last_name"]
    assert engine.answer(Question("Email address", kind="email")).value == p["email"]
    assert engine.answer(Question("Mobile phone", kind="tel")).value == p["phone"]
    assert engine.answer(Question("LinkedIn Profile")).value == p["linkedin"]
    assert engine.answer(Question("Company name")).value == "talQen"


def test_unknown_screening_answers_are_never_guessed(engine):
    a = engine.answer(Question("Will you require visa sponsorship?", kind="radio", options=["Yes", "No"], required=True))
    if load_profile()["screening_answers"].get("requires_visa_sponsorship_outside_kenya") is None:
        assert a.needs_user and a.value is None
    a = engine.answer(Question("What is your favourite colour?", required=True))
    assert a.needs_user and a.value is None


def test_eeo_uses_profile_then_declines_and_consent_is_left_for_user(engine):
    a = engine.answer(Question("Gender", kind="select", options=["Male", "Female", "Decline to self-identify"]))
    assert a.value == "Male"  # Kelly's answer in master_profile demographics
    a = engine.answer(Question("Sexual orientation", kind="select", options=["Straight", "Gay", "Decline to self-identify"]))
    assert a.value == "Decline to self-identify"  # not in the profile: declined
    a = engine.answer(Question("I agree to the privacy policy", kind="checkbox", required=True))
    assert a.consent and a.value is None


def test_numbers_and_options(engine):
    a = engine.answer(Question("How many years of experience do you have?", kind="select", options=["0-2", "3-5", "6+"]))
    assert a.value == "3-5"
    assert pick_option(["Yes", "No"], False) == "No"


def test_written_answers_need_user_without_llm(engine):
    a = engine.answer(Question("Why do you want to work here?", kind="textarea", required=True))
    assert a.needs_user and a.value is None


def _browser_ok():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(executable_path=os.environ.get("JOBSPRINT_CHROMIUM_PATH") or None).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_stage_stops_before_submit_then_submit_after_approval(tmp_path, monkeypatch):
    from src.automation import form_stager, form_submitter
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    monkeypatch.setattr(form_submitter, "CONFIRM", form_submitter.CONFIRM)
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    url = (FIX / "apply_form.html").as_uri()
    with db.session(ws.db_path) as c:
        jid = db.upsert_job(c, {"url": url, "source": "lever:test", "title": "Backend Engineer", "company": "Acme",
                                "description": "Python Django REST APIs", "status": "QUALIFIED", "age_hours": 2,
                                "priority_score": 0.9, "fit_score": 0.8})
    monkeypatch.setattr(form_stager, "application_url", lambda job: url)
    counts = form_stager.run(ws, limit=5)
    assert sum(counts.values()) == 1
    with db.session(ws.db_path) as c:
        s = dict(c.execute("SELECT * FROM staged_forms").fetchone())
        job = dict(c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone())
    answers = {a["label"]: a for a in json.loads(s["answers_json"])}
    assert answers["First Name"]["filled"] and answers["Email"]["filled"] and answers["Resume/CV"]["filled"]
    assert answers["Gender"]["value"] == "Male"
    assert answers["I agree to the privacy policy"]["consent"] and not answers["I agree to the privacy policy"]["filled"]
    assert s["state"] in ("NEEDS_USER_INPUT", "READY_TO_SUBMIT") and job["status"] == s["state"]
    assert "SUBMITTED" not in (s["state"], job["status"])  # staging never submits
    shot = Path(json.loads('"' + str(ws.screenshots).replace("\\", "/") + '"'))
    assert any(shot.glob("*_staged.png"))

    # Approval: user answers the sponsorship question and ticks consent on the review page.
    res = form_submitter.submit(ws, s["id"], overrides={"Will you now or in the future require visa sponsorship?": "No"},
                                consents=["I agree to the privacy policy"], confirm_wait_ms=5000)
    assert res["ok"], res
    with db.session(ws.db_path) as c:
        assert c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "SUBMITTED"


def test_review_api(tmp_path):
    try:
        from fastapi.testclient import TestClient
        TestClient  # noqa: B018
    except Exception:
        pytest.skip("test client dependency not installed")
    from src.web.server import create_app
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        jid = db.upsert_job(c, {"url": "https://x/1", "source": "remotive", "title": "AI Trainer", "status": "MANUAL_APPLY", "age_hours": 1})
        c.execute("INSERT INTO staged_forms(job_id, form_url, state, created_at) VALUES (?,?,?,?)", (jid, "https://x/1", "MANUAL_APPLY", db.now_iso()))
    client = TestClient(create_app(ws))
    assert "Job Sprint" in client.get("/").text
    items = client.get("/api/staged").json()
    assert len(items) == 1 and items[0]["state"] == "MANUAL_APPLY"
    assert client.post(f"/api/skip/{items[0]['id']}").json()["ok"]
    assert client.get("/api/staged").json() == []
    assert client.get("/files/screenshot/999").status_code == 404


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_follows_listing_apply_link_to_form(tmp_path, monkeypatch):
    from src.automation import form_stager
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    listing = (FIX / "listing.html").as_uri()
    with browser(ws) as ctx:
        page = ctx.new_page()
        url = form_stager._follow_to_ats(page, {"url": listing})
    assert url and url.endswith("apply_form.html")


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_skips_anchor_jumps_and_auto_apply_then_clicks_button(tmp_path, monkeypatch):
    from src.automation import form_stager
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    with browser(ws) as ctx:
        url = form_stager._follow_to_ats(ctx.new_page(), {"url": (FIX / "listing2.html").as_uri()})
    assert url and url.endswith("apply_form.html")


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_signup_pages_are_never_treated_as_applications(tmp_path, monkeypatch):
    from src.automation import form_stager
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    with browser(ws) as ctx:
        page = ctx.new_page()
        page.goto((FIX / "signup.html").as_uri())
        assert form_stager._account_page(page) and not form_stager._has_form(page)
        assert form_stager._follow_to_ats(page, {"url": (FIX / "signup.html").as_uri()}) is None
