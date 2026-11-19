"""Step 10: LinkedIn Easy Apply (fake LinkedIn pages) and picking the real application form."""
import pytest

from src import database as db
from src.workspace import ensure_workspace
from tests.test_step3 import _browser_ok

needs_browser = pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")

JOB = """<html><body><nav><form><input type=text name=q placeholder=Search></form></nav>
<h1>Data Analyst</h1><button class="jobs-apply-button" aria-label="Easy Apply to Data Analyst at Acme" onclick="openModal()">Easy Apply</button>
<div id=root></div>
<script>
let step = 0;
const steps = [
 `<label for=ph>Mobile phone number</label><input id=ph required value="+254700000000">
  <label for=cv>Upload resume</label><input id=cv type=file name=resume>
  <button aria-label="Continue to next step" onclick="next()">Next</button>`,
 `<label for=yrs>How many years of work experience do you have with Python?</label>
  <select id=yrs required><option value="">Select an option</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select>
  <fieldset><legend>Are you legally authorized to work in Kenya?</legend>
   <label><input type=radio name=auth value=Yes required>Yes</label><label><input type=radio name=auth value=No>No</label></fieldset>
  <button aria-label="Review your application" onclick="next()">Review</button>`,
 `<label><input type=checkbox id=follow checked> Follow Acme to stay up to date with their page.</label>
  <button aria-label="Submit application" onclick="done()">Submit application</button>`];
function openModal(){ document.getElementById('root').innerHTML = '<div class="jobs-easy-apply-modal" role="dialog"><button aria-label="Dismiss">x</button><div id=s></div></div>'; render(); }
function render(){ document.getElementById('s').innerHTML = steps[step]; }
function next(){ if (step === 0 && !document.getElementById('cv').files.length) return; step++; render(); }
function done(){ window.followed = document.getElementById('follow').checked; document.body.innerHTML = '<h2>Your application was sent to Acme!</h2>'; }
</script></body></html>"""


@needs_browser
def test_easy_apply_fills_every_step_and_submits(tmp_path, monkeypatch):
    from src.automation import form_stager
    from src.automation import linkedin_easy_apply as lea
    from src.automation.browser_manager import browser
    from src.cv_tailor.single_template_tailor import load_profile
    from src.intelligence.answer_engine import AnswerEngine
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    monkeypatch.setattr(lea, "signed_in", lambda page: True)
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        jid = c.execute("INSERT INTO jobs(source,url,title,company,status,first_seen_at,updated_at,description) VALUES (?,?,?,?,?,?,?,?)",
                        ("linkedin", "https://www.linkedin.com/jobs/view/4492045815/", "Data Analyst", "Acme", "QUALIFIED",
                         db.now_iso(), db.now_iso(), "SQL, Python, Power BI dashboards")).lastrowid
    profile = load_profile()
    with browser(ws) as ctx, db.session(ws.db_path) as c:
        ctx.route("**/*", lambda r: r.fulfill(body=JOB, content_type="text/html"))
        page = ctx.new_page()
        job = dict(c.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone())
        state = form_stager.stage_job(page, c, ws, job, AnswerEngine(profile, use_llm=False), profile)
        assert state == "SUBMITTED"
        assert page.evaluate("window.followed") is False  # "Follow company" was unticked
        row = c.execute("SELECT * FROM staged_forms WHERE job_id = ?", (jid,)).fetchone()
        assert "application was sent" in row["receipt_json"].lower()
        assert lea.sent_today(c) == 1


def test_linkedin_job_url_normalised():
    from src.automation.linkedin_easy_apply import job_url
    assert job_url("https://www.linkedin.com/jobs/search/?currentJobId=4492045815&x=1") == "https://www.linkedin.com/jobs/view/4492045815/"
    assert job_url("https://ke.linkedin.com/jobs/view/mechanical-engineer-at-fluor-4492045815?trk=x") == "https://www.linkedin.com/jobs/view/4492045815/"


@needs_browser
def test_discovery_picks_the_application_form_not_the_search_box(tmp_path, monkeypatch):
    from src.automation import form_fields
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    with browser(ws) as ctx:
        page = ctx.new_page()
        page.set_content("<form><input name=q type=text placeholder=Search></form>"
                         "<form><label for=a>First name</label><input id=a required><label for=b>Last name</label><input id=b required>"
                         "<label for=c>Resume</label><input id=c type=file></form>")
        assert [f["label"] for f in form_fields.discover(page)] == ["First name", "Last name", "Resume"]


def test_we_work_remotely_is_blocked():
    from src.automation.form_stager import blocked_site
    assert blocked_site({"url": "https://weworkremotely.com/remote-jobs/acme-dev"}) == "weworkremotely.com"
    assert blocked_site({"url": "https://x.com/a", "apply_url": "https://www.weworkremotely.com/a"}) == "weworkremotely.com"
    assert blocked_site({"url": "https://jobs.lever.co/acme/1"}) is None


def test_demographic_answers_from_profile():
    import json
    from src.intelligence.answer_engine import AnswerEngine, Question
    e = AnswerEngine(json.load(open("config/master_profile.json")), use_llm=False)
    q = lambda lab, o: e.answer(Question(label=lab, kind="select", options=o, required=True)).value
    assert q("Veteran status", ["I am a protected veteran", "I am not a protected veteran", "Decline"]) == "I am not a protected veteran"
    assert q("Disability", ["Yes, I have a disability", "No, I do not have a disability", "I don't want to answer"]) == "No, I do not have a disability"
    assert q("Race", ["Asian", "Black or African American", "Decline to self-identify"]) == "Black or African American"


def test_updater_retries_a_flaky_test_once(tmp_path):
    import sys
    from src import updater
    (tmp_path / "test_x.py").write_text(
        "import pathlib\n"
        "def test_flaky():\n"
        "    m = pathlib.Path(__file__).with_name('ran')\n"
        "    first = not m.exists(); m.write_text('1')\n"
        "    assert not first\n"
        "def test_ok():\n    assert True\n")
    ok, summary = updater.run_tests(tmp_path, sys.executable)
    assert ok and "retried" in summary
    (tmp_path / "test_y.py").write_text("def test_broken():\n    assert False\n")
    ok, summary = updater.run_tests(tmp_path, sys.executable)
    assert not ok and "test_broken" in summary


def test_portal_sign_up_does_not_lock_the_database(tmp_path, monkeypatch):
    """With a real EMAIL_ADDRESS the stager tries to create a portal account; saving its password must not hit
    'database is locked' (it did on the laptop, so every sign-up page ended FAILED)."""
    import pytest
    from tests.test_step4 import FIX, _browser_ok
    if not _browser_ok():
        pytest.skip("no browser")
    from src import database as db
    from src.automation import form_stager
    from src.workspace import ensure_workspace
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    monkeypatch.setenv("EMAIL_ADDRESS", "someone@example.com")
    monkeypatch.setattr("src.automation.portal_accounts.protect", lambda pw, ws: "x", raising=False)
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    url = (FIX / "signup.html").as_uri()
    with db.session(ws.db_path) as c:
        db.upsert_job(c, {"url": url, "source": "himalayas", "title": "Data Engineer", "company": "Acme",
                          "description": "Python SQL", "status": "QUALIFIED", "age_hours": 2, "priority_score": 0.9})
    monkeypatch.setattr(form_stager, "application_url", lambda job: url)
    res = form_stager.run(ws, limit=1)
    assert "FAILED" not in res, res


def test_react_select_dropdowns_hidden_toggle_and_ashby_resume(tmp_path, monkeypatch):
    import json
    import pytest
    from tests.test_step4 import FIX, _browser_ok
    if not _browser_ok():
        pytest.skip("no browser")
    from src.automation import form_fields
    from src.automation.browser_manager import browser
    from src.intelligence.answer_engine import AnswerEngine
    from src.workspace import ensure_workspace
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    cv = tmp_path / "cv.docx"
    cv.write_bytes(b"x")
    engine = AnswerEngine(json.load(open("config/master_profile.json")), use_llm=False)
    job = {"title": "AI Trainer", "company": "Acme", "location": "Remote", "description": "Remote role"}
    with browser(ws) as ctx:
        page = ctx.new_page()
        page.goto((FIX / "combo_form.html").as_uri())
        fields = form_fields.discover(page)
        by = {f["label"]: f for f in fields}
        assert by["Race"]["options"][1] == "Black or African American"
        assert not any("requiredInput" in f["label"] for f in fields) and len([f for f in fields if f["kind"] == "combobox"]) == 3
        for f in fields:
            a = engine.answer(form_fields.to_question(f), job, str(cv))
            if f["label"] == "Location (City)":
                a.value = "Nairobi, Kenya"
            if f["label"].startswith("I am over 18"):
                a.value = True
            form_fields.fill_field(page, f, a.value)
        vals = page.eval_on_selector_all(".select__single-value", "els => els.map(e => e.textContent)")
        assert "No" in vals and "Black or African American" in vals and "Nairobi, Nairobi County, Kenya" in vals
        assert page.locator("#adult").is_checked()
        assert page.locator("._active").all_inner_texts() == ["Yes", "No"]  # over 18: Yes; worked at Acme before: No
        assert by["Are you over 18?"]["kind"] == "buttons"
        assert page.locator("#_systemfield_resume").evaluate("e => e.files.length") == 1
        assert form_fields.verify(page) == []


def test_per_employer_limits_and_review_mode(tmp_path):
    from src.automation import auto_submitter as a
    assert a.site_key("https://job-boards.greenhouse.io/sourcegraph91/jobs/1") == "job-boards.greenhouse.io/sourcegraph91"
    assert a.site_key("https://jobs.ashbyhq.com/ElevenLabs/x/application") == "jobs.ashbyhq.com/elevenlabs"
    assert a.site_key("https://careers.acme.com/jobs/1") == "careers.acme.com"
    assert a.review_mode({"review_from": "2000-01-01T00:00:00+00:00"}) is True
    assert a.review_mode({"review_from": "2999-01-01T00:00:00+00:00"}) is False
    assert a.review_mode({"review_from": ""}) is False
    from src.settings import load_settings
    assert "review_from" in load_settings()


def test_newsletters_never_get_recruiter_replies(tmp_path):
    from src.email_agent.imap_scanner import is_recruiter
    from src.email_agent.recruiter_replies import machine_sender
    m = {"from_addr": "fantasy@email.premierleague.com", "subject": "Don't miss the deadline", "text": "new opportunity", "bulk": False}
    assert not is_recruiter(m) and machine_sender(m["from_addr"], m["subject"])
    assert not is_recruiter({"from_addr": "jane@acme.com", "subject": "Interview", "text": "interview", "bulk": True})
    assert is_recruiter({"from_addr": "jane@acme.com", "subject": "Interview", "text": "interview", "bulk": False})


def test_accommodation_inbox_refused_at_send_time(tmp_path):
    from src.automation.email_apply import blocked_recipient
    assert blocked_recipient("reasonableaccommodations@airbnb.com")
    assert blocked_recipient("jobs@brightermonday.co.ke")
    assert blocked_recipient("hr@acme.co.ke") is None


def test_linkedin_signed_in_search_cards(tmp_path, monkeypatch):
    import pytest
    from tests.test_step4 import FIX, _browser_ok
    if not _browser_ok():
        pytest.skip("no browser")
    from src.automation.browser_manager import browser
    from src.discovery import linkedin_browser as lb
    from src.workspace import ensure_workspace
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    assert "f_AL=true" in lb.search_url("ai trainer", lb.KENYA_GEO, False) and "f_WT=2" in lb.search_url("x", lb.WORLDWIDE_GEO, True)
    assert lb._plain_term('"a" OR "b" OR "c" OR "d"') == '"a" OR "b" OR "c"'
    with browser(ensure_workspace(tmp_path / "JobSprintAgent")) as ctx:
        page = ctx.new_page()
        page.goto((FIX / "linkedin_search.html").as_uri())
        cs = lb.cards(page)
    first = next(c for c in cs if c["id"] == "4123456789")
    assert first["title"] == "AI Data Specialist" and first["company"] == "RWS" and first["posted"] == "2026-11-19"
    assert next(c for c in cs if c["id"] == "4123456790")["applied"]
    assert lb.fetch(None, {}, {}) == []  # a hidden browser never searches LinkedIn


def test_ats_board_auto_kind_tries_each_system():
    from src.discovery import ats_api_fetcher as f

    class R:
        def __init__(self, code, data=None):
            self.status_code, self._d = code, data
        def json(self):
            return self._d

    class S:
        def get(self, url, timeout=None):
            return R(200, {"jobs": []}) if "ashbyhq" in url else R(404)
    found, miss = f._fetch_board(S(), {"kind": "auto", "board": "acme", "name": "Acme"}, 5)
    assert found == [] and miss is None
    class N:
        def get(self, url, timeout=None):
            return R(404)
    assert f._fetch_board(N(), {"kind": "auto", "board": "nope"}, 5)[1] == "nope (auto:nope)"
