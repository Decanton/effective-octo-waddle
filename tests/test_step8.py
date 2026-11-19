"""Step 8: why this job, application record, results and follow-ups."""
import json
from datetime import datetime, timedelta, timezone

from src import database as db
from src.cv_tailor.single_template_tailor import load_profile
from src.workspace import ensure_workspace


def _iso(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def test_explain_lists_met_and_missing_requirements():
    from src.intelligence.explain import explain, requirement_lines
    desc = ("About us\nWe build things.\nRequirements\n- 3+ years of Python and SQL\n- Experience with HL7 integrations\n"
            "- Fluent in Mandarin Chinese\nBenefits\n- Free lunch every day here")
    assert requirement_lines(desc) == ["3+ years of Python and SQL", "Experience with HL7 integrations", "Fluent in Mandarin Chinese"]
    x = explain({"title": "Senior Backend Engineer", "description": desc, "age_hours": 5, "applicant_count": 12, "priority_score": 0.6},
                load_profile())
    met = [r["text"] for r in x["requirements_met"]]
    assert "3+ years of Python and SQL" in met and "Experience with HL7 integrations" in met
    assert [r["text"] for r in x["requirements_missing"]] == ["Fluent in Mandarin Chinese"]
    assert any("Senior" in r for r in x["reasons"]) and any("5 hours" in r for r in x["reasons"])


def _seed(ws):
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        def job(title, company, src="jobwebkenya"):
            return c.execute("INSERT INTO jobs(source,url,title,company,status,first_seen_at,updated_at,description) VALUES (?,?,?,?,?,?,?,?)",
                             (src, f"https://x/{title}", title, company, "SUBMITTED", _iso(10), _iso(10), "post text")).lastrowid
        a, b, f = job("Data Analyst", "Acme Ltd"), job("Engineer", "Beta"), job("Dev", "Gamma", "greenhouse")
        rec = json.dumps({"method": "email", "to": "hr@acme.co.ke", "subject": "Application: Data Analyst", "message_id": "<m1@x>"})
        c.execute("INSERT INTO staged_forms(job_id, form_url, state, created_at, submitted_at, receipt_json) VALUES (?,?,?,?,?,?)",
                  (a, "mailto:hr@acme.co.ke", "SUBMITTED", _iso(9), _iso(9), rec))
        c.execute("INSERT INTO staged_forms(job_id, form_url, state, created_at, submitted_at, receipt_json) VALUES (?,?,?,?,?,?)",
                  (b, "mailto:jobs@beta.com", "SUBMITTED", _iso(9), _iso(9), rec.replace("acme.co.ke", "beta.com")))
        c.execute("INSERT INTO staged_forms(job_id, form_url, state, created_at, submitted_at) VALUES (?,?,?,?,?)",
                  (f, "https://boards.greenhouse.io/g/1", "SUBMITTED", _iso(9), _iso(9)))
        cols = {r[1] for r in c.execute("PRAGMA table_info(recruiter_emails)")}
        row = {"message_id": "r1", "from_addr": "talent@beta.com", "from_name": "Beta Talent", "subject": "Interview invitation",
               "category": "interview_request", "received_at": _iso(2), "state": "DRAFTED", "created_at": _iso(2)}
        row = {k: v for k, v in row.items() if k in cols}
        c.execute(f"INSERT INTO recruiter_emails({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))


def test_results_funnel_and_follow_ups(tmp_path):
    from src import analytics
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    _seed(ws)
    with db.session(ws.db_path) as c:
        r = analytics.results(c)
    assert r["funnel"]["sent"] == 3 and r["funnel"]["replied"] == 1 and r["funnel"]["interviews"] == 1
    by = {a["company"]: a for a in r["applications"]}
    assert by["Beta"]["status"] == "interview" and not by["Beta"]["follow_up_due"]
    assert by["Acme Ltd"]["follow_up_due"] and not by["Gamma"]["follow_up_due"] or by["Gamma"]["method"] == "form"


def test_follow_up_sent_once_in_thread(tmp_path, monkeypatch):
    from src.email_agent import followups
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    _seed(ws)
    monkeypatch.setenv("EMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "x")
    sent = []

    class S:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, *a): pass
        def send_message(self, m): sent.append(m)
    cfg = {"enabled": True, "after_days": 7, "auto_send": True}
    with db.session(ws.db_path) as c:
        out = followups.run(c, cfg, load_profile(), smtp_factory=S)
    assert out["sent"] == 1 and sent[0]["To"] == "hr@acme.co.ke" and sent[0]["In-Reply-To"] == "<m1@x>"
    assert sent[0]["Subject"] == "Re: Application: Data Analyst"
    with db.session(ws.db_path) as c:
        assert followups.run(c, cfg, load_profile(), smtp_factory=S)["sent"] == 0  # never twice
    assert len(sent) == 1


def test_record_and_results_api(tmp_path):
    from fastapi.testclient import TestClient
    from src.web.server import create_app
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    _seed(ws)
    with db.session(ws.db_path) as c:
        c.execute("UPDATE staged_forms SET posting_snapshot = ? WHERE id = 2", (json.dumps({"description": "As posted", "captured_at": _iso(9)}),))
    cl = TestClient(create_app(ws))
    rec = cl.get("/api/record/2").json()
    assert rec["posting"]["description"] == "As posted" and rec["receipt"]["to"] == "hr@beta.com"
    assert rec["replies"][0]["subject"] == "Interview invitation"
    assert cl.get("/api/results").json()["funnel"]["sent"] == 3
    assert "requirements_met" in cl.get("/api/explain/1").json()
