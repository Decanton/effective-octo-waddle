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


def test_rediscovery_never_requeues_a_sent_job(tmp_path):
    from src import database as db
    from src.workspace import ensure_workspace
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        jid = db.upsert_job(c, {"url": "https://jobs.ashbyhq.com/gravie/1", "source": "ashby:gravie", "title": "Manager EDI", "status": "QUALIFIED"})
        db.set_job_status(c, jid, "SUBMITTED")
        db.upsert_job(c, {"url": "https://jobs.ashbyhq.com/gravie/1", "source": "ashby:gravie", "title": "Manager EDI", "status": "QUALIFIED"})
        assert c.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == "SUBMITTED"


def test_title_only_jobs_and_phone_format():
    from src.cv_tailor.single_template_tailor import load_profile
    from src.intelligence.answer_engine import phone_for
    from src.intelligence.job_ranker import fit_score
    assert fit_score({"title": "Data Engineer", "description": ""}, load_profile())[0] >= 0.3
    assert fit_score({"title": "Sales Manager", "description": ""}, load_profile())[0] < 0.22
    assert phone_for("+254 706 921 015", "Tel (ex: 712 123456)") == "706 921 015"


def test_review_mode_sends_no_replies_or_follow_ups(tmp_path, monkeypatch):
    from src.email_agent import followups, recruiter_replies
    monkeypatch.setenv("JOBSPRINT_NO_SUBMIT", "1")
    assert recruiter_replies.auto_send(None, {"auto_send": True}) == {"sent": 0, "waiting": 0}

    class C:
        pass
    monkeypatch.setattr(followups.analytics, "results", lambda *a, **k: {"applications": [{"follow_up_due": True, "method": "email", "id": 1}]})
    assert followups.run(C(), {"auto_send": True}, {})["sent"] == 0


def test_send_all_ready_works_in_review_mode(tmp_path, monkeypatch):
    """Review mode holds automatic sends, never the review page's 'Send all ready'."""
    from src import database as db
    from src.automation import auto_submitter as a
    from src.settings import load_settings
    from src.workspace import ensure_workspace
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        jid = db.upsert_job(c, {"url": "https://jobs.lever.co/acme/1", "source": "lever:acme", "title": "Dev", "company": "Acme", "status": "READY_TO_SUBMIT"})
        c.execute("INSERT INTO staged_forms(job_id, form_url, answers_json, state, created_at) VALUES (?,?,?,?,?)",
                  (jid, "https://jobs.lever.co/acme/1/apply", "[]", "READY_TO_SUBMIT", db.now_iso()))
    monkeypatch.setattr(a, "review_mode", lambda settings=None: True)
    sent = []
    fake = lambda ws, sid, *x, **k: sent.append(sid) or {"ok": True}
    s = load_settings()
    assert a.run(ws, s, submit_fn=fake)["sent"] == 0 and not sent
    assert a.run(ws, s, submit_fn=fake, approved=True)["sent"] == 1 and sent == [1]


def test_sign_in_helpers_and_more_blocked_inboxes(tmp_path, monkeypatch):
    from src.automation.email_apply import blocked_recipient
    from src.automation.portal_accounts import login_id
    assert login_id("owuordickens0001@gmail.com") == "owuordickens0001"
    assert blocked_recipient("belonging@booksy.com") and blocked_recipient("hr@equitybank.co.ke") is None
    import pytest
    from tests.test_step4 import _browser_ok
    if not _browser_ok():
        pytest.skip("no browser")
    from src.automation import form_fields
    from src.automation.browser_manager import browser
    from src.workspace import ensure_workspace
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    with browser(ensure_workspace(tmp_path / "JobSprintAgent")) as ctx:
        page = ctx.new_page()
        page.set_content('<form><label for="a">Name</label><input id="a" value="x"><div><label for="resume">Resume/CV *</label>'
                         '<input type="file" id="resume" required style="display:none"><span>Dickens_CV.docx</span></div></form>')
        assert form_fields.verify(page) == []  # the site emptied the input after uploading; the file's name shows


def test_sign_in_presses_the_forms_button_and_linkedin_cards_without_links(tmp_path, monkeypatch):
    import pytest
    from tests.test_step4 import _browser_ok
    if not _browser_ok():
        pytest.skip("no browser")
    from src import database as db
    from src.automation import portal_accounts
    from src.automation.browser_manager import browser
    from src.discovery import linkedin_browser
    from src.workspace import ensure_workspace
    from tests.test_step5 import _pages_route
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        portal_accounts.save_credentials(c, ws, "careers.org.test", "me@example.com", "pw-123", True)
    login = ('<header><a href="https://careers.org.test/register">Register</a></header><h1>Login</h1>'
             '<form onsubmit="event.preventDefault(); if(document.querySelector(\'#p\').value===\'pw-123\') location=\'https://careers.org.test/home\'">'
             '<label for=e>Email</label><input id=e type=email><label for=p>Password</label><input id=p type=password>'
             '<button type=submit>Login</button></form><a>Forgot password?</a>')
    pages = {"https://careers.org.test/auth/login": login,
             "https://careers.org.test/register": "<h1>Create account</h1><input type=email><input type=password><input type=password>",
             "https://careers.org.test/home": "<h1>Welcome back</h1>"}
    with browser(ws) as ctx:
        _pages_route(ctx, pages)
        page = ctx.new_page()
        page.goto("https://careers.org.test/auth/login")
        res = portal_accounts.sign_in_or_register(page, ws, "me@example.com", {"enabled": True, "code_wait_seconds": 1},
                                                  profile={"personal": {}}, wait_for_code=lambda *a, **k: None)
        assert res == "ok" and page.url == "https://careers.org.test/home"
        page.goto("https://careers.org.test/auth/login")
        page.fill("#p", "")
        with db.session(ws.db_path) as c:
            portal_accounts.save_credentials(c, ws, "careers.org.test", "me@example.com", "wrong", True)
        res = portal_accounts.sign_in_or_register(page, ws, "me@example.com", {"enabled": True, "code_wait_seconds": 1},
                                                  profile={"personal": {}}, wait_for_code=lambda *a, **k: None)
        assert res == "failed"  # still on the login form: not reported as signed in
        page.set_content('<ul><li data-occludable-job-id="4012345678"><div class="job-card-container">'
                         '<strong>AI Data Trainer</strong><div class="artdeco-entity-lockup__subtitle">Acme</div></div></li>'
                         '<li data-occludable-job-id="4012345679"></li></ul>')
        got = linkedin_browser.cards(page)
        assert [(c["id"], c["title"], c["company"]) for c in got] == [("4012345678", "AI Data Trainer", "Acme")]


def test_linkedin_results_list_parser_and_guest_url():
    from src.discovery import linkedin_browser as lb
    html = ('<li><div class="base-card" data-entity-urn="urn:li:jobPosting:4099887766">'
            '<a class="base-card__full-link" href="https://ke.linkedin.com/jobs/view/ai-trainer-at-acme-4099887766?trk=x"></a>'
            '<h3 class="base-search-card__title"> AI Trainer </h3><h4 class="base-search-card__subtitle"><a>Acme</a></h4>'
            '<span class="job-search-card__location">Nairobi, Kenya</span><time datetime="2026-11-20">1 day ago</time></div></li>'
            '<li><div>no job here</div></li>')
    got = lb.parse_guest(html)
    assert got == [{"id": "4099887766", "title": "AI Trainer", "company": "Acme", "location": "Nairobi, Kenya",
                    "posted": "2026-11-20", "applied": False}]
    u = lb.guest_url("ai trainer", lb.KENYA_GEO, False, 48, 25)
    assert u.startswith(lb.GUEST_API + "?keywords=ai+trainer") and "f_TPR=r172800" in u and u.endswith("&start=25")


def test_waits_for_a_form_that_draws_its_fields_late(tmp_path, monkeypatch):
    import pytest
    from tests.test_step4 import _browser_ok
    if not _browser_ok():
        pytest.skip("no browser")
    from src.automation import form_stager
    from src.automation.browser_manager import browser
    from src.workspace import ensure_workspace
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    with browser(ensure_workspace(tmp_path / "JobSprintAgent")) as ctx:
        page = ctx.new_page()
        page.set_content('<form id=f><input id=a></form><script>setTimeout(() => { for (const n of ["first_name","last_name","email"]) '
                         '{ const i = document.createElement("input"); i.id = n; document.getElementById("f").appendChild(i); } }, 2500)</script>')
        form_stager._wait_form_ready(page)
        assert page.locator("input").count() == 4


def test_run_lock_one_at_a_time_and_clears_dead_runs(tmp_path):
    import pytest
    from src import run_lock
    from src.workspace import ensure_workspace
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    with run_lock.held(ws, "stage/send"):
        assert run_lock.holder(ws)["what"] == "stage/send"
        assert not run_lock.acquire(ws, "daily run")
        with pytest.raises(RuntimeError):
            with run_lock.held(ws, "daily run", wait_minutes=0):
                pass
    assert run_lock.holder(ws) is None and run_lock.acquire(ws, "x")
    run_lock.path(ws).write_text("999999\nold run")  # a crashed run's lock
    assert run_lock.holder(ws) is None and run_lock.acquire(ws, "new")
    run_lock.release(ws)


def test_one_job_is_never_sent_twice(tmp_path):
    from src import database as db
    from src.automation import auto_submitter
    from src.workspace import ensure_workspace
    from tests.test_step4 import _staged
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        _staged(c, "https://boards.greenhouse.io/acme/jobs/1", title="Data Engineer")
        _staged(c, "https://boards.greenhouse.io/acme/jobs/1?gh_src=x", title="Data Engineer")
    sent = []
    from src.settings import load_settings
    st = load_settings()
    st["auto_submit"] = {**st["auto_submit"], "enabled": True, "only_hosts": [""], "max_per_day": 99}
    auto_submitter.run(ws, st, submit_fn=lambda ws_, sid, *a, **k: sent.append(sid) or {"state": "SUBMITTED"}, approved=True)
    assert len(sent) == 1


def test_standard_yes_no_questions_typed_in_a_box():
    from src.cv_tailor.single_template_tailor import load_profile
    from src.intelligence import answer_engine as ae
    e = ae.AnswerEngine(load_profile(), use_llm=False)
    for label, kind in (("Have you worked at Customer.io before?", "text"),
                        ("Do you have any post-employment restrictions?", "textarea"),
                        ("Have you previously been employed by GitLab?", "combobox")):
        a = e.answer(ae.Question(label=label, kind=kind, options=[], required=True), {"title": "x"}, "")
        assert (a.value, a.source) == ("No", "standard"), label


def test_school_results_from_profile():
    from src.cv_tailor.single_template_tailor import load_profile
    from src.intelligence import answer_engine as ae
    e = ae.AnswerEngine(load_profile(), use_llm=False)
    ask = lambda label: e.answer(ae.Question(label=label, kind="textarea", options=[], required=True), {"title": "x"}, "")
    assert ask("How did you perform in mathematics at high school?").value == "A"
    assert ask("How did you do in your native language in high school?").value == "A"
    assert ask("Which university did you attend?").value == "University of Nairobi"
    assert ask("What was your bachelor's degree result?").value.startswith("Second Class Honours, Upper Division")


def test_company_limits_repeats_and_page_furniture_in_titles(tmp_path):
    from src import database as db
    from src.automation import auto_submitter, company_rules
    from src.discovery.common import new_job
    from src.workspace import ensure_workspace
    from tests.test_step4 import _staged
    assert new_job(title="Jobs similar to @ Senior Software Developer", company="Jobs similar to")["title"] == "Senior Software Developer"
    assert new_job(title="x", company="Jobs similar to")["company"] is None
    assert company_rules.norm_company("Supabase, Inc.") == company_rules.norm_company("supabase")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        for i in range(3):  # three Supabase applications already sent
            sid = _staged(c, f"https://jobs.ashbyhq.com/supabase/{i}", title=f"Old role {i}")
            c.execute("UPDATE staged_forms SET state = 'SUBMITTED', submitted_at = ? WHERE id = ?", (db.now_iso(), sid))
        c.execute("UPDATE jobs SET company = 'Supabase'")
        new = _staged(c, "https://jobs.ashbyhq.com/supabase/9", title="New role")
        c.execute("UPDATE jobs SET company = 'Supabase' WHERE id = (SELECT job_id FROM staged_forms WHERE id = ?)", (new,))
        rep = _staged(c, "https://apply.workable.com/acme/1", title="Old role 0")  # Acme: a different company, fine
        dup = _staged(c, "https://apply.workable.com/supabase/1", title="Old role 1")
        c.execute("UPDATE jobs SET company = 'Supabase Inc' WHERE id = (SELECT job_id FROM staged_forms WHERE id = ?)", (dup,))
        cfg = {"default": {"max": 3, "days": 60}}
        assert "limit" in company_rules.blocked(c, {"id": 999, "company": "Supabase", "title": "Another"}, cfg)
        assert "already applied" in company_rules.blocked(c, {"id": 999, "company": "SUPABASE", "title": "old role 1"}, cfg)
    from src.settings import load_settings
    st = load_settings()
    st["auto_submit"] = {**st["auto_submit"], "enabled": True, "only_hosts": [""], "max_per_day": 99, "per_site_default": 99}
    sent = []
    auto_submitter.run(ws, st, submit_fn=lambda ws_, sid, *a, **k: sent.append(sid) or {"state": "SUBMITTED"}, approved=True)
    assert sent == [rep]


def test_waiting_forms_over_a_company_limit_are_taken_off(tmp_path):
    from src import database as db
    from src.automation import company_rules
    from src.workspace import ensure_workspace
    from tests.test_step4 import _staged
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        sent = _staged(c, "https://jobs.ashbyhq.com/supabase/0", title="Old")
        c.execute("UPDATE staged_forms SET state = 'SUBMITTED', submitted_at = ? WHERE id = ?", (db.now_iso(), sent))
        ids = [_staged(c, f"https://jobs.ashbyhq.com/supabase/{i}", title=f"Role {i}", prio=1 - i / 10) for i in range(1, 5)]
        c.execute("UPDATE jobs SET company = 'Supabase'")
        assert company_rules.prune_ready(c, {"default": {"max": 3, "days": 60}}) == 2
        left = [r[0] for r in c.execute("SELECT id FROM staged_forms WHERE state = 'READY_TO_SUBMIT' ORDER BY id")]
        assert left == ids[:2]  # the two best-ranked stay


def test_canonical_questionnaire_never_guessed():
    from src.cv_tailor.single_template_tailor import load_profile
    from src.intelligence import answer_engine as ae
    e = ae.AnswerEngine(load_profile(), use_llm=False)
    rank = ["Cannot recall", "Not a strength", "Top 50% at school", "Top 20%", "Top 10%", "Top 5%"]
    ask = lambda label, kind="textarea", opts=(): e.answer(ae.Question(label=label, kind=kind, options=list(opts), required=True),
                                                            {"title": "x", "company": "Canonical"}, "")
    degree = ask("What was your bachelor’s university degree result, or expected result if you have not yet graduated? Please "
                 "include the grading system e.g. ‘2:1’. We have hired outstanding individuals who did not attend or "
                 "complete university. If this describes you, please continue and enter ‘no degree’.")
    assert degree.value.startswith("Second Class Honours")
    math = ask("How did you perform in mathematics at high school?", "combobox", rank)
    assert math.value is None and math.needs_user  # never "Cannot recall" from the grade "A"
    assert ask("Please share your rationale or evidence for the high school performance selections above, e.g. JAMB, "
               "matriculation results, IB results etc. in your region").value != "Nairobi County"
    n = ask("In the past ten years, since you graduated your first undergraduate degree, how many companies have you worked for?",
            "combobox", [str(i) for i in range(10)])
    assert n.value != "Bachelor's degree"
    assert ae.pick_option(rank, "A") is None
