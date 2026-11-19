"""Step 4: inbox reading, recruiter drafts, automatic sending rules, sign-in tracking."""
import json
import os
from email.message import EmailMessage
from pathlib import Path

import pytest

from src import database as db
from src.automation import auto_submitter
from src.email_agent import imap_scanner, recruiter_replies
from src.settings import load_settings
from src.workspace import ensure_workspace
from tests.test_step3 import FIX, _browser_ok

PROFILE = {"personal": {"full_name": "Dickens Owuor Owino", "email": "d@example.com"}, "summaries": {}, "screening_answers": {}}


def _raw(frm, subject, html=None, text=None, date="Thu, 19 Nov 2026 08:00:00 +0000"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = frm, "me@example.com", subject, date, f"<{subject[:5]}@x>"
    m.set_content(text or "plain")
    if html:
        m.add_alternative(html, subtype="html")
    return m.as_bytes()


def _now():
    from datetime import datetime, timezone
    return datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def test_linkedin_alert_becomes_jobs():
    html = """<a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc">AI Data Specialist</a>
              <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?x=1">View job</a>
              <a href="https://click.example.com/r?url=https%3A%2F%2Fjobs.lever.co%2Facme%2Fabc-123">Backend Engineer</a>
              <a href="https://www.linkedin.com/settings">Settings</a>"""
    m = imap_scanner.parse_message(_raw("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
                                        "\"data analyst\": 12 new jobs in Kenya", html), now=_now())
    assert imap_scanner.is_job_alert(m) and not imap_scanner.is_recruiter(m)
    jobs = imap_scanner.alert_jobs(m)
    urls = {j["url"]: j for j in jobs}
    assert set(urls) == {"https://www.linkedin.com/jobs/view/4012345678", "https://jobs.lever.co/acme/abc-123"}
    assert urls["https://www.linkedin.com/jobs/view/4012345678"]["title"] == "AI Data Specialist"
    assert all(j["age_hours"] == pytest.approx(2.0) and j["source"].startswith("email:") for j in jobs)


def test_recruiter_detection_and_categories():
    m = imap_scanner.parse_message(_raw("Jane Recruiter <jane@acme.com>", "Interview for the Data Engineer role",
                                        text="Hi Dickens, could you share your availability for an interview this week?"), now=_now())
    assert imap_scanner.is_recruiter(m, "me@example.com") and imap_scanner.category(m) == "interview_request"
    news = imap_scanner.parse_message(_raw("Medium Digest <noreply@medium.com>", "Hiring trends in 2026"), now=_now())
    assert not imap_scanner.is_recruiter(news)
    for frm, subj in (("Hashlist <team@hashlist.com>", "Dickens Owuor, completed your profile yet?"),
                      ("Dice <applyonline@dice.com>", "Application for Entry-Level Data Analyst at Dhalite Inc. sent"),
                      ("Acme Careers <careers@acme.com>", "Thank you for applying to the Data role")):
        assert not imap_scanner.is_recruiter(imap_scanner.parse_message(_raw(frm, subj, text="your application"), now=_now()))


def test_draft_and_send_only_on_request(tmp_path, monkeypatch):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    monkeypatch.setattr(recruiter_replies.llm_client, "available", lambda: False)
    m = {"message_id": "<1@x>", "from_addr": "jane@acme.com", "from_name": "Jane Doe", "subject": "Interview",
         "text": "When are you free?", "category": "interview_request", "received_at": "2026-11-19T08:00:00+00:00"}
    with db.session(ws.db_path) as c:
        assert recruiter_replies.save(c, [m], PROFILE) == 1
        assert recruiter_replies.save(c, [m], PROFILE) == 0  # no duplicates
        row = c.execute("SELECT * FROM recruiter_emails").fetchone()
        assert row["state"] == "DRAFTED" and "Hi Jane" in row["draft_reply"] and "[FILL IN" in row["draft_reply"]
        # A draft with a gap is refused; nothing is sent.
        monkeypatch.setenv("EMAIL_ADDRESS", "me@example.com")
        monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh")
        assert not recruiter_replies.send(c, row["id"], row["draft_reply"])["ok"]
        sent = []

        class FakeSMTP:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, u, p): assert p == "abcdefgh"
            def send_message(self, msg): sent.append(msg)
        res = recruiter_replies.send(c, row["id"], "Hi Jane, Tuesday 10am EAT works. Dickens", smtp_factory=FakeSMTP)
        assert res["ok"] and sent[0]["To"] == "jane@acme.com" and sent[0]["In-Reply-To"] == "<1@x>"
        assert sent[0]["Subject"] == "Re: Interview"
        assert not recruiter_replies.send(c, row["id"], "again", smtp_factory=FakeSMTP)["ok"]  # never twice


def _staged(c, url, state="READY_TO_SUBMIT", answers=(), error=None, prio=0.5):
    jid = db.upsert_job(c, {"url": url + "#job", "source": "greenhouse:x", "title": "Data Engineer", "company": "Acme",
                            "status": state, "age_hours": 2, "priority_score": prio})
    cur = c.execute("INSERT INTO staged_forms(job_id, form_url, answers_json, state, error, created_at) VALUES (?,?,?,?,?,?)",
                    (jid, url, json.dumps(list(answers)), state, error, db.now_iso()))
    return cur.lastrowid


def test_auto_send_rules_and_daily_cap(tmp_path):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    consent = {"label": "I agree to the privacy policy", "consent": True, "required": True}
    with db.session(ws.db_path) as c:
        ok1 = _staged(c, "https://boards.greenhouse.io/acme/jobs/1", prio=0.9)
        ok2 = _staged(c, "https://jobs.lever.co/acme/2/apply", prio=0.8)
        ok3 = _staged(c, "https://jobs.ashbyhq.com/acme/3/application", prio=0.7)
        other = _staged(c, "https://careers.example.com/apply/4")
        cap = _staged(c, "https://boards.greenhouse.io/acme/jobs/5", error="CAPTCHA on this form: you'll solve it")
        cons = _staged(c, "https://boards.greenhouse.io/acme/jobs/6", answers=[consent])
        needs = _staged(c, "https://boards.greenhouse.io/acme/jobs/7", state="NEEDS_USER_INPUT")
    calls = []

    def fake_submit(ws_, sid, overrides, consents, confirm_wait_ms):
        calls.append((sid, consents))
        with db.session(ws_.db_path) as c:
            c.execute("UPDATE staged_forms SET state='SUBMITTED', submitted_at=? WHERE id=?", (db.now_iso(), sid))
        return {"ok": True}

    s = load_settings()
    s["auto_submit"].update(enabled=True, max_per_day=2, accept_required_consents=False)
    counts = auto_submitter.run(ws, s, submit_fn=fake_submit)
    assert [c[0] for c in calls] == [ok1, ok2] and counts["sent"] == 2  # best first, capped at 2
    assert counts["waiting_for_you"] >= 4
    calls.clear()
    assert auto_submitter.run(ws, s, submit_fn=fake_submit)["sent"] == 0 and not calls  # cap holds for the day
    # Consent forms go only when you allow it; other sites, CAPTCHAs and incomplete forms never auto-send.
    s["auto_submit"].update(max_per_day=50, accept_required_consents=True)
    auto_submitter.run(ws, s, submit_fn=fake_submit)
    assert {c[0] for c in calls} == {ok3, cons} and dict(calls)[cons] == ["I agree to the privacy policy"]
    s["auto_submit"]["enabled"] = False
    calls.clear()
    assert auto_submitter.run(ws, s, submit_fn=fake_submit)["sent"] == 0 and not calls
    assert {other, cap, needs}  # untouched


def test_settings_defaults(tmp_path):
    s = load_settings(tmp_path / "missing.yaml")
    assert s["auto_submit"]["enabled"] is False and s["email"]["lookback_hours"] == 24
    real = load_settings()
    assert real["auto_submit"]["max_per_day"] >= 1 and "greenhouse.io" in real["auto_submit"]["only_hosts"]


def test_missing_boards_are_summarised(monkeypatch):
    from src.discovery import ats_api_fetcher as ats

    class R:
        def __init__(self, code, data=None): self.status_code, self._d = code, data
        def json(self): return self._d

    def fake_get(self, url, timeout):
        return R(404) if "nope" in url else R(200, {"jobs": []})
    monkeypatch.setattr(ats.requests.Session, "get", fake_get)
    cfg = {"ats_boards": [{"name": "A", "kind": "greenhouse", "board": "ok"}, {"name": "B", "kind": "ashby", "board": "nope"}]}
    assert ats.fetch(cfg) == []


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_signup_page_is_tracked_as_needs_login(tmp_path, monkeypatch):
    from src.automation import browser_manager, form_stager
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    url = (FIX / "signup.html").as_uri()
    with db.session(ws.db_path) as c:
        db.upsert_job(c, {"url": url, "source": "himalayas", "title": "Data Engineer", "company": "Acme",
                          "description": "Python SQL", "status": "QUALIFIED", "age_hours": 2, "priority_score": 0.9})
    monkeypatch.setattr(form_stager, "application_url", lambda job: url)
    assert form_stager.run(ws, limit=1) == {"NEEDS_LOGIN": 1}
    with db.session(ws.db_path) as c:
        assert c.execute("SELECT state FROM staged_forms").fetchone()[0] == "NEEDS_LOGIN"
    assert any(u == url for _, u in browser_manager.pages_to_sign_in(ws))


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_hidden_login_popup_does_not_hide_a_real_form(tmp_path, monkeypatch):
    from src.automation import form_stager
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    with browser(ws) as ctx:
        page = ctx.new_page()
        page.goto((FIX / "hidden_login.html").as_uri())
        assert not form_stager._account_page(page) and form_stager._has_form(page)


from src.automation import email_apply  # noqa: E402


@pytest.mark.parametrize("text,to", [
    ("Interested candidates should send their CV and cover letter to careers@acme.co.ke by 30th Nov.", "careers@acme.co.ke"),
    ("How to apply: email your resume to Jobs@Acme.com with the subject 'Data Analyst - Your Name'.", "jobs@acme.com"),
    ("Applications (CV + cover letter) should be addressed to hr@twiga.com.", "hr@twiga.com"),
    ("If you need an accommodation during the application process, contact accommodations@acme.com.", None),
    ("Questions about privacy? Write to privacy@acme.com. Apply via the form below with your CV.", None),
    ("Contact noreply@acme.com to send your CV.", None),
    ("We love Python. Reach the team at team@acme.com for anything.", None),
])
def test_finds_apply_by_email_address(text, to):
    found = email_apply.find_apply_email(text)
    assert (found or {}).get("to") == to


def test_subject_hint():
    f = email_apply.find_apply_email("Send your CV to jobs@acme.com. Subject line: Junior Data Engineer Application.\n")
    assert f["subject_hint"] == "Junior Data Engineer Application"
    p = {"personal": {"full_name": "Dickens Owuor Owino"}}
    assert email_apply.subject_for({"title": "Data Engineer"}, None, p) == "Application: Data Engineer - Dickens Owuor Owino"
    assert email_apply.subject_for({"title": "Data Engineer"}, "the job title and your name", p) == "Data Engineer - Dickens Owuor Owino"


def test_email_application_staged_and_sent_once(tmp_path, monkeypatch):
    from src.cv_tailor.single_template_tailor import build_cv, load_profile
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    monkeypatch.setattr(email_apply.llm_client, "available", lambda: False)
    profile = load_profile()
    job = {"url": "https://brightermonday.co.ke/listings/1", "source": "email:brightermonday.co.ke", "title": "Data Analyst",
           "company": "Twiga", "description": "Python SQL. Send your CV and cover letter to hr@twiga.com", "status": "QUALIFIED",
           "age_hours": 3, "priority_score": 0.8}
    with db.session(ws.db_path) as c:
        job["id"] = db.upsert_job(c, job)
        cv_path, cv = build_cv(job["title"] + "\n" + job["description"], ws.tailored_cvs / "t.docx", profile=profile)
        cv_id = db.add_tailored_cv(c, job["id"], cv.track, str(cv_path), cv.matched_keywords)
        found = email_apply.find_apply_email(job["description"])
        assert email_apply.stage(c, ws, job, cv_id, cv_path, cv.track, found, profile) == "READY_TO_SUBMIT"
        sid = c.execute("SELECT id FROM staged_forms").fetchone()[0]
    monkeypatch.setenv("EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "pw")
    sent = []

    class FakeSMTP:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def send_message(self, m): sent.append(m)
    res = email_apply.send(ws, sid, smtp_factory=FakeSMTP)
    assert res["ok"], res
    m = sent[0]
    assert m["To"] == "hr@twiga.com" and "Data Analyst" in m["Subject"]
    names = [p.get_filename() for p in m.iter_attachments()]
    assert names == ["Dickens_Owuor_Owino_CV.docx", "Dickens_Owuor_Owino_Cover_Letter.docx"]
    body = m.get_body(("plain",)).get_content()
    assert "Twiga" in body and "Dickens Owuor Owino" in body
    assert not email_apply.send(ws, sid, smtp_factory=FakeSMTP)["ok"] and len(sent) == 1  # never twice
    with db.session(ws.db_path) as c:
        assert auto_submitter.sent_today(c, by_email=True) == 1 and auto_submitter.sent_today(c) == 0


def test_email_letter_with_placeholder_is_not_sent(tmp_path, monkeypatch):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        jid = db.upsert_job(c, {"url": "u", "source": "x", "title": "T", "status": "READY_TO_SUBMIT", "age_hours": 1})
        cur = c.execute("INSERT INTO staged_forms(job_id, form_url, answers_json, state, created_at) VALUES (?,?,?,?,?)",
                        (jid, "mailto:hr@x.com", json.dumps([{"label": "To", "value": "hr@x.com"}, {"label": "Subject", "value": "s"},
                                                            {"label": "Cover letter", "value": "Dear [Name]"}]), "READY_TO_SUBMIT", db.now_iso()))
    assert "placeholder" in email_apply.send(ws, cur.lastrowid)["error"]


def test_auto_send_handles_email_applications_with_own_cap(tmp_path):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        e1 = _staged(c, "mailto:hr@a.com", prio=0.9)
        e2 = _staged(c, "mailto:hr@b.com", prio=0.8)
        f1 = _staged(c, "https://boards.greenhouse.io/acme/jobs/1", prio=0.7)
    calls = []

    def fake_submit(ws_, sid, overrides, consents, confirm_wait_ms):
        calls.append(sid)
        with db.session(ws_.db_path) as c:
            c.execute("UPDATE staged_forms SET state='SUBMITTED', submitted_at=? WHERE id=?", (db.now_iso(), sid))
        return {"ok": True}
    s = load_settings()
    s["auto_submit"].update(enabled=False)
    s["email_applications"].update(enabled=True, max_per_day=1)
    auto_submitter.run(ws, s, submit_fn=fake_submit)
    assert calls == [e1]  # forms off, email cap 1
    s["auto_submit"].update(enabled=True, max_per_day=5)
    auto_submitter.run(ws, s, submit_fn=fake_submit)
    assert calls == [e1, f1] and e2 not in calls


def test_alert_card_gives_title_company_and_place():
    html = """<a href="https://www.linkedin.com/comm/jobs/view/4099999999/?t=1"><table><tr><td>Data Acquisition Engineer</td></tr>
              <tr><td>SPIRO · Nairobi (On-site)</td></tr></table></a>
              <a href="https://www.linkedin.com/comm/jobs/view/4088888888/"><p>AI Data Specialist</p><p>RWS</p><p>Kenya (Remote)</p></a>"""
    m = imap_scanner.parse_message(_raw("LinkedIn <jobalerts-noreply@linkedin.com>", "New jobs for you", html), now=_now())
    jobs = {j["title"]: j for j in imap_scanner.alert_jobs(m)}
    assert jobs["Data Acquisition Engineer"]["company"] == "SPIRO" and jobs["Data Acquisition Engineer"]["location"] == "Nairobi (On-site)"
    assert jobs["AI Data Specialist"]["company"] == "RWS" and jobs["AI Data Specialist"]["is_remote"]


def test_linkedin_page_fills_company_and_location():
    from src.discovery.jobspy_fetcher import parse_linkedin_job_html
    html = """<h1 class="top-card-layout__title">Data Engineer</h1><a class="topcard__org-name-link">Acme Ltd</a>
              <span class="topcard__flavor--bullet">Nairobi, Kenya</span><span class="posted-time-ago__text">3 hours ago</span>"""
    out = parse_linkedin_job_html(html, {"title": "x", "company": None})
    assert out["company"] == "Acme Ltd" and out["location"] == "Nairobi, Kenya" and out["age_hours"] == 3
    assert "company" not in parse_linkedin_job_html(html, {"company": "Known"})


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_continue_with_google_picks_the_job_account(tmp_path, monkeypatch):
    from src.automation import google_signin
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    pages = {
        "https://jobs.example.test/login": "<h1>Sign in</h1><input type=password><a href='https://accounts.google.com/o/oauth2/auth'>Continue with Google</a>",
        "https://accounts.google.com/o/oauth2/auth": "<div data-email='other@gmail.com'>Other</div>"
            "<div data-email='me@example.com' onclick=\"location='https://accounts.google.com/consent'\">Me</div>",
        "https://accounts.google.com/consent": "<button onclick=\"location='https://jobs.example.test/apply'\">Continue</button>",
        "https://jobs.example.test/apply": "<form><label for=a>First Name</label><input id=a><label for=b>Last Name</label><input id=b>"
            "<label for=c>Email</label><input id=c type=email></form>",
        "https://accounts.google.com/pw": "<input type=email>",
    }
    with browser(ws) as ctx:
        ctx.route("**/*", lambda r: r.fulfill(body=pages.get(r.request.url.split("?")[0], "<p>404</p>"), content_type="text/html"))
        page = ctx.new_page()
        page.goto("https://jobs.example.test/login")
        assert google_signin.try_google(page, "me@example.com") == "ok"
        assert page.url == "https://jobs.example.test/apply"
        pages["https://jobs.example.test/login"] = "<a href='https://accounts.google.com/pw'>Sign in with Google</a>"
        page.goto("https://jobs.example.test/login")
        assert google_signin.try_google(page, "me@example.com") == "needs_password"  # never types a password
        page.set_content("<p>No buttons</p>")
        assert google_signin.try_google(page, "me@example.com") == "no_button"


def test_recruiter_replies_auto_send_only_complete_drafts(tmp_path, monkeypatch):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    monkeypatch.setattr(recruiter_replies.llm_client, "available", lambda: False)
    monkeypatch.setenv("EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "pw")
    base = {"from_name": "Jane", "text": "x", "received_at": "2026-11-19T08:00:00+00:00"}
    msgs = [{**base, "message_id": "<a>", "from_addr": "a@acme.com", "subject": "Data role", "category": "other"},
            {**base, "message_id": "<b>", "from_addr": "b@acme.com", "subject": "Interview", "category": "interview_request"},
            {**base, "message_id": "<c>", "from_addr": "c@acme.com", "subject": "Update", "category": "rejection"},
            {**base, "message_id": "<d>", "from_addr": "d@acme.com", "subject": "Another role", "category": "other"}]
    sent = []

    class FakeSMTP:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def send_message(self, m): sent.append(m["To"])
    with db.session(ws.db_path) as c:
        recruiter_replies.save(c, msgs, PROFILE)
        cfg = load_settings()["recruiter_replies"]
        assert recruiter_replies.auto_send(c, {**cfg, "auto_send": False}, FakeSMTP)["sent"] == 0 and not sent
        out = recruiter_replies.auto_send(c, {**cfg, "auto_send": True, "max_per_day": 1}, FakeSMTP)
        assert sent == ["a@acme.com"] and out["waiting"] == 3  # interview has a [FILL IN] gap, rejection skipped, cap 1
        recruiter_replies.auto_send(c, {**cfg, "auto_send": True, "max_per_day": 5}, FakeSMTP)
        assert sent == ["a@acme.com", "d@acme.com"]


def test_chrome_profile_copy_skips_passwords_and_history(tmp_path):
    from src.automation import chrome_profile
    src = tmp_path / "User Data"
    (src / "Profile 2" / "Network").mkdir(parents=True)
    (src / "Profile 2" / "Cache").mkdir()
    (src / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Default": {"name": "Person 1"}, "Profile 2": {"name": "Dickens Owuor"}}}}))
    for f in ("Network/Cookies", "Preferences", "Login Data", "History", "Web Data", "Cache/x"):
        (src / "Profile 2" / f).write_text("x")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    assert chrome_profile.find_profile("dickens owuor", src) == "Profile 2"
    dest = chrome_profile.clone(ws, "Dickens Owuor", src, check_running=False)
    got = {p.relative_to(dest / "Profile 2").as_posix() for p in (dest / "Profile 2").rglob("*") if p.is_file()}
    assert got == {"Network/Cookies", "Preferences"} and (dest / "Local State").exists()
    assert chrome_profile.cloned(ws) == (dest, "Profile 2")
    with pytest.raises(LookupError):
        chrome_profile.find_profile("Nobody", src)


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_onboarding_and_search_pages_are_not_forms(tmp_path, monkeypatch):
    from src.automation import form_stager
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    pages = {"https://weworkremotely.com/job-seekers/onboarding/step_1": "<input type=search><input name=a><input name=b><input type=email>",
             "https://example.test/careers": "<input name=q><input name=a><input type=email name=email>",
             "https://example.test/apply": "<input name=a><input name=b><input type=email><input type=file>"}
    with browser(ws) as ctx:
        ctx.route("**/*", lambda r: r.fulfill(body=pages.get(r.request.url, "<p>x</p>"), content_type="text/html"))
        page = ctx.new_page()
        page.goto("https://weworkremotely.com/job-seekers/onboarding/step_1")
        assert form_stager._account_page(page) and not form_stager._has_form(page)
        page.goto("https://example.test/careers")
        assert not form_stager._has_form(page)  # no CV upload: not an application
        page.goto("https://example.test/apply")
        assert form_stager._has_form(page)


def test_work_authorization_notice_and_address_answers():
    from src.cv_tailor.single_template_tailor import load_profile
    from src.intelligence.answer_engine import AnswerEngine, Question
    e = AnswerEngine(load_profile(), use_llm=False)
    yn = ["Yes", "No"]
    remote = {"location": "Remote", "is_remote": True}
    us = {"location": "New York, NY", "is_remote": False}
    a = lambda label, job=None, kind="radio", opts=yn: e.answer(Question(label, kind, opts, True), job)
    assert a("Will you now or in the future require visa sponsorship to work in the United States?").value == "Yes"
    assert a("Are you legally authorized to work in the United States?").value == "No"
    assert a("Are you authorized to work in Kenya?").value == "Yes"
    assert a("Are you legally authorized to work in the country where you reside?").value == "Yes"
    assert a("Do you require sponsorship?", remote).value == "No"
    assert a("Do you require sponsorship?", us).value == "Yes"
    assert a("Are you eligible to work in this location?", {"location": "Nairobi, Kenya"}).value == "Yes"
    assert a("Notice period", kind="select", opts=["Immediately", "1 week", "1 month"]).value == "1 week"
    assert a("Notice period", kind="select", opts=["Immediately", "1 month", "3 months"]).value == "Immediately"
    assert a("How much notice do you need?", kind="text", opts=[]).value == "1 week"
    assert a("Earliest start date", kind="date", opts=[]).value.count("-") == 2
    assert a("Street address", kind="text", opts=[]).value == "Waiyaki Way, Westlands"
    assert a("City", kind="text", opts=[]).value == "Nairobi"
    assert a("Country", kind="text", opts=[]).value == "Kenya"


def test_salary_answers():
    from src.cv_tailor.single_template_tailor import load_profile
    from src.intelligence.answer_engine import AnswerEngine, Question
    e = AnswerEngine(load_profile(), use_llm=False)
    ke, abroad = {"location": "Nairobi, Kenya"}, {"location": "Remote", "is_remote": True}
    assert e.answer(Question("Expected monthly salary", "text"), ke).value == "USD 800 per month"
    assert e.answer(Question("Expected monthly salary", "text"), abroad).value == "USD 2,500 per month"
    assert e.answer(Question("Expected annual salary (USD)", "number"), abroad).value == "30000"
    assert e.answer(Question("Expected hourly rate", "text"), abroad).value == "USD 20 per hour"
    assert e.answer(Question("Salary expectation in KES per month", "text"), ke).value == "KES 103,000 per month"
    assert e.answer(Question("Salary expectations", "select", ["$0-1,000", "$1,001-2,000", "$2,001-3,000"]), abroad).value == "$2,001-3,000"


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_closed_jobs_are_skipped_not_staged(tmp_path, monkeypatch):
    from src.automation import form_stager
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    page = tmp_path / "closed.html"
    page.write_text("<h1>Data Engineer</h1><p>This job is no longer accepting applications.</p>")
    with db.session(ws.db_path) as c:
        db.upsert_job(c, {"url": page.as_uri(), "source": "lever:x", "title": "Data Engineer", "company": "Acme",
                          "description": "Python", "status": "QUALIFIED", "age_hours": 2, "priority_score": 0.9})
    monkeypatch.setattr(form_stager, "application_url", lambda job: page.as_uri())
    assert form_stager.run(ws, limit=1) == {"SKIPPED": 1}


def test_same_email_application_is_never_sent_twice_from_two_listings(tmp_path, monkeypatch):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        a = db.upsert_job(c, {"url": "u1", "source": "x", "title": "Data Analyst", "status": "SUBMITTED", "age_hours": 1})
        b = db.upsert_job(c, {"url": "u2", "source": "y", "title": "data analyst", "status": "READY_TO_SUBMIT", "age_hours": 1})
        c.execute("INSERT INTO staged_forms(job_id, form_url, state, submitted_at, created_at) VALUES (?,?,?,?,?)",
                  (a, "mailto:hr@twiga.com", "SUBMITTED", db.now_iso(), db.now_iso()))
        cur = c.execute("INSERT INTO staged_forms(job_id, form_url, answers_json, state, created_at) VALUES (?,?,?,?,?)",
                        (b, "mailto:hr@twiga.com", json.dumps([{"label": "To", "value": "hr@twiga.com"}, {"label": "Subject", "value": "s"},
                                                              {"label": "Cover letter", "value": "Dear Hiring Manager"}]), "READY_TO_SUBMIT", db.now_iso()))
    res = email_apply.send(ws, cur.lastrowid, smtp_factory=lambda: (_ for _ in ()).throw(AssertionError("must not send")))
    assert not res["ok"] and "already" in res["error"]


def test_only_one_auto_reply_per_sender_per_day(tmp_path, monkeypatch):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    monkeypatch.setattr(recruiter_replies.llm_client, "available", lambda: False)
    monkeypatch.setenv("EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "pw")
    base = {"from_name": "Jane", "text": "x", "received_at": "2026-11-19T08:00:00+00:00", "from_addr": "jane@acme.com", "category": "other"}
    sent = []

    class FakeSMTP:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def send_message(self, m): sent.append(m["To"])
    with db.session(ws.db_path) as c:
        recruiter_replies.save(c, [{**base, "message_id": "<1>", "subject": "Role A"}, {**base, "message_id": "<2>", "subject": "Role B"},
                                   {**base, "message_id": "<3>", "subject": "Hi", "from_addr": "me@example.com"}], PROFILE)
        recruiter_replies.auto_send(c, {"auto_send": True, "max_per_day": 10, "skip_categories": []}, FakeSMTP)
    assert sent == ["jane@acme.com"]


def test_daily_run_refuses_to_start_twice(tmp_path, monkeypatch):
    import run_sprint
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    monkeypatch.setattr(run_sprint, "setup", lambda: ws)
    db.init_db(ws.db_path)
    (ws.data / "sprint.lock").write_text("123")
    assert run_sprint.main(["--no-review"]) == 1
    assert (ws.data / "sprint.lock").exists()  # the other run's lock is left alone
