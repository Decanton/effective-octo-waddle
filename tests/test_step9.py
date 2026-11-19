"""Step 9: action log, per-site limits, reposts and stale jobs, CV changes."""
from datetime import datetime, timedelta, timezone

from src import database as db
from src.workspace import ensure_workspace


def _iso(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _job(c, title, company, status="QUALIFIED", url=None, days=0):
    return c.execute("INSERT INTO jobs(source,url,title,company,status,first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                     ("t", url or f"https://x/{title}/{company}/{days}", title, company, status, _iso(days), _iso(days))).lastrowid


def test_reposts_are_skipped_and_old_jobs_dropped(tmp_path):
    from src.discovery import pipeline
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        _job(c, "Data Analyst", "Acme Ltd", status="SUBMITTED", days=5)
        old = _job(c, "Engineer", "Beta", days=3)
        assert pipeline.repost_of(c, {"title": "Data  Analyst!", "company": "acme ltd", "url": "https://other/1"})
        assert not pipeline.repost_of(c, {"title": "Data Engineer", "company": "Acme Ltd", "url": "https://other/2"})
        c.execute("UPDATE jobs SET status = 'REJECTED_STALE' WHERE status = 'QUALIFIED' AND first_seen_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-2 days')")
        assert c.execute("SELECT status FROM jobs WHERE id = ?", (old,)).fetchone()[0] == "REJECTED_STALE"


def test_per_site_daily_limit(tmp_path):
    from src.automation import auto_submitter
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        for i in range(3):
            jid = _job(c, f"Role {i}", "Acme", status="READY_TO_SUBMIT")
            c.execute("INSERT INTO staged_forms(job_id, form_url, state, created_at, answers_json) VALUES (?,?,?,?,?)",
                      (jid, f"https://boards.greenhouse.io/acme/jobs/{i}", "READY_TO_SUBMIT", _iso(), "[]"))
    sent = []

    def fake_submit(ws_, sid, overrides, consents, confirm_wait_ms=0):
        with db.session(ws_.db_path) as c:
            c.execute("UPDATE staged_forms SET state='SUBMITTED', submitted_at=? WHERE id=?", (_iso(), sid))
        sent.append(sid)
        return {"ok": True}
    settings = {"auto_submit": {"enabled": True, "max_per_day": 20, "only_hosts": ["greenhouse.io"], "accept_required_consents": False,
                                "confirm_wait_seconds": 1, "per_site_default": 2, "per_site_per_day": {}},
                "email_applications": {"enabled": False}}
    auto_submitter.run(ws, settings, submit_fn=fake_submit)
    assert len(sent) == 2  # the third waits for tomorrow


def test_actions_are_logged_and_listed(tmp_path):
    from fastapi.testclient import TestClient
    from src.web.server import create_app
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        db.audit(c, "email_application", "hr@acme.co.ke", "Data Analyst @ Acme")
        db.audit(c, "follow_up", "hr@acme.co.ke", "Data Analyst @ Acme")
    acts = TestClient(create_app(ws)).get("/api/activity").json()
    assert [a["action"] for a in acts] == ["follow_up", "email_application"]


def test_cv_changes_side_by_side(tmp_path):
    from fastapi.testclient import TestClient
    from src.cv_tailor.single_template_tailor import build_cv
    from src.web.server import create_app
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    path, cv = build_cv("Senior Python backend engineer: Django, REST APIs, PostgreSQL, Docker, AWS, microservices",
                        ws.tailored_cvs / "t.docx", track="software")
    with db.session(ws.db_path) as c:
        jid = _job(c, "Backend Engineer", "Acme", status="READY_TO_SUBMIT")
        cid = db.add_tailored_cv(c, jid, "software", str(path), cv.matched_keywords)
        c.execute("INSERT INTO staged_forms(job_id, cv_id, form_url, state, created_at) VALUES (?,?,?,?,?)",
                  (jid, cid, "https://boards.greenhouse.io/acme/jobs/1", "READY_TO_SUBMIT", _iso()))
    x = TestClient(create_app(ws)).get("/api/cvdiff/1").json()
    assert x["track"] == "software" and x["rows"] and all({"standard", "tailored", "kind"} <= set(r) for r in x["rows"])


def test_finds_role_on_company_ashby_board(monkeypatch):
    from src.discovery import company_boards as cb
    cb._cache.clear()
    ashby = {"jobs": [{"id": "1", "title": "Full-Stack Engineer (Backend Leaning) - Creative Agents", "isListed": True,
                       "jobUrl": "https://jobs.ashbyhq.com/ElevenLabs/35813150", "applyUrl": "https://jobs.ashbyhq.com/ElevenLabs/35813150/application",
                       "publishedAt": "2026-11-19T10:00:00Z", "descriptionPlain": "x"},
                      {"id": "2", "title": "Account Executive", "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/ElevenLabs/2"}]}

    class R:
        def __init__(self, ok, data=None): self.ok, self._d = ok, data
        def json(self): return self._d

    def get(self, url, timeout=None):
        return R(True, ashby) if "ashbyhq.com" in url and "/elevenlabs" in url else R(False)
    monkeypatch.setattr("requests.Session.get", get)
    url = cb.find({"title": "Full-Stack Engineer (Backend Leaning) - Creative Agents", "company": "ElevenLabs"})
    assert url == "https://jobs.ashbyhq.com/ElevenLabs/35813150/application"
    assert cb.find({"title": "Mechanical Engineer", "company": "ElevenLabs"}) is None
    assert cb.slugs("Fluor Corporation")[0] == "fluorcorporation" and "fluor" in cb.slugs("Fluor Corporation")


def test_accommodation_and_privacy_inboxes_never_used():
    from src.automation.email_apply import find_apply_email
    t = "If you need assistance applying or an accommodation, send your CV request to reasonableaccommodations@airbnb.com"
    assert find_apply_email(t) is None
    assert find_apply_email("Please send your CV and cover letter to careers-privacy@acme.com") is None
