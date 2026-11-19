from datetime import date, datetime, timedelta, timezone

import pytest

from src import database as db
from src.cv_tailor.single_template_tailor import load_profile
from src.discovery import ats_api_fetcher as ats
from src.discovery import pipeline
from src.discovery.common import load_yaml, new_job
from src.discovery.freshness import is_fresh, parse_age_hours, parse_applicants
from src.discovery.jobspy_fetcher import parse_linkedin_job_html
from src.intelligence.job_ranker import low_applicant_score, rank
from src.workspace import ensure_workspace

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("value,expected", [
    ("Just posted", 0), ("Today", 0), ("2 hours ago", 2), ("an hour ago", 1), ("45 minutes ago", 0.75),
    ("1 day ago", 24), ("2 days ago", 48), ("30+ days ago", 721), ("3 weeks ago", 504),
    ("2026-11-19T09:00:00Z", 3), ("2026-11-18T11:00:00+00:00", 25), (NOW - timedelta(hours=5), 5),
    (int((NOW - timedelta(hours=10)).timestamp() * 1000), 10),
])
def test_parse_age(value, expected):
    assert parse_age_hours(value, NOW) == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("value", [None, "", "recently", "Posted on Monday", float("nan"), "yesterday"])
def test_unparseable_age_is_none(value):
    assert parse_age_hours(value, NOW) is None


def test_bare_dates_need_source_side_filter():
    assert parse_age_hours(date(2026, 11, 19), NOW) is None
    assert parse_age_hours(date(2026, 11, 19), NOW, date_only_ok=True) == pytest.approx(12)
    assert parse_age_hours("2026-11-18", NOW, date_only_ok=True) == 24
    assert parse_age_hours("2026-11-16", NOW, date_only_ok=True) == 72


def test_is_fresh_strict():
    assert is_fresh(0) and is_fresh(48) and not is_fresh(48.01) and not is_fresh(None)


@pytest.mark.parametrize("text,n", [
    ("Over 200 applicants", 201), ("Be among the first 25 applicants", 24), ("37 applicants", 37),
    ("12 people clicked apply", 12), ("No applicants yet", 0), ("Applicants: 1,204", 1204), ("Apply now", None),
])
def test_parse_applicants(text, n):
    assert parse_applicants(text) == n


def test_linkedin_html():
    html = '''<span class="posted-time-ago__text">3 hours ago</span>
              <figcaption class="num-applicants__caption">Be among the first 25 applicants</figcaption>
              <div class="show-more-less-html__markup"><p>Evaluate LLM outputs in Python.</p></div>'''
    out = parse_linkedin_job_html(html, {})
    assert out["age_hours"] == 3 and out["applicant_count"] == 24 and "LLM" in out["description"]


def test_ats_parsers():
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    gh = ats.parse_greenhouse({"jobs": [{"id": 1, "absolute_url": "https://x/1", "title": "AI Trainer",
                                         "location": {"name": "Remote"}, "content": "&lt;p&gt;Hi&lt;/p&gt;",
                                         "first_published": fresh, "updated_at": fresh}]}, "Co", "co")
    assert gh[0]["is_remote"] and gh[0]["age_hours"] == pytest.approx(2, abs=0.1) and gh[0]["description"] == "Hi"
    gh2 = ats.parse_greenhouse({"jobs": [{"id": 2, "absolute_url": "https://x/2", "title": "T", "updated_at": fresh}]}, "Co", "co")
    assert gh2[0]["age_hours"] is None  # updated_at alone is not a posting date
    ms = int((datetime.now(timezone.utc) - timedelta(hours=30)).timestamp() * 1000)
    lv = ats.parse_lever([{"id": "a", "hostedUrl": "https://l/a", "text": "Backend Engineer", "createdAt": ms,
                           "categories": {"location": "Nairobi"}, "descriptionPlain": "Python"}], "L", "l")
    assert lv[0]["age_hours"] == pytest.approx(30, abs=0.1)
    ab = ats.parse_ashby({"jobs": [{"id": "b", "jobUrl": "https://a/b", "title": "Prompt Engineer", "isRemote": True,
                                    "publishedAt": fresh, "descriptionPlain": "LLM"}]}, "A", "a")
    assert ab[0]["is_remote"] and ab[0]["age_hours"] < 3


def _job(**kw):
    base = dict(source="test", url=f"https://j/{kw.get('title', 'x')}", title="AI Trainer", company="Acme",
                location="Remote", is_remote=True, age_hours=2,
                description="LLM evaluation, prompt engineering, RLHF annotation, Python code review")
    base.update(kw)
    return new_job(**base)


def test_rank_rules_and_order():
    profile, cfg = load_profile(), load_yaml("search_queries.yaml")
    jobs = [
        _job(title="LLM Evaluator", applicant_count=300, url="u1"),
        _job(title="AI Trainer (Python)", applicant_count=8, url="u2"),
        _job(title="AI Trainer stale", age_hours=50, url="u3"),
        _job(title="AI Trainer undated", age_hours=None, url="u4"),
        _job(title="Director of AI", url="u5"),
        _job(title="Prompt Engineer", description="Must be a US citizen. LLM work.", url="u6"),
        _job(title="Mechanical Engineer", is_remote=False, location="Berlin, Germany",
             description="SolidWorks FEA", url="u7"),
        _job(title="Mechanical Engineer", is_remote=False, location="Nairobi, Kenya",
             description="SolidWorks FEA AutoCAD maintenance", url="u8"),
        _job(title="AI Trainer (Python)", applicant_count=8, url="u9"),  # duplicate title+company
    ]
    out = {j["url"]: j for j in rank(jobs, profile, cfg)}
    assert out["u3"]["status"] == "REJECTED_STALE" and out["u4"]["status"] == "REJECTED_STALE"
    assert out["u5"]["status"] == "REJECTED_KNOCKOUT" and out["u6"]["status"] == "REJECTED_KNOCKOUT"
    assert out["u7"]["status"] == "REJECTED_KNOCKOUT" and out["u8"]["status"] == "QUALIFIED"
    assert out["u8"]["track"] == "mechanical"
    assert out["u9"]["status"] == "SKIPPED"
    assert out["u2"]["priority_score"] > out["u1"]["priority_score"]  # fewer applicants wins


def test_unknown_applicants_neutral():
    assert low_applicant_score(None) == 0.5 and low_applicant_score(5) == 1 and low_applicant_score(500) < 0.1


def test_pipeline_saves_and_exports(tmp_path, monkeypatch):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    monkeypatch.setattr(pipeline.ats_api_fetcher, "fetch", lambda cfg: [_job(title="AI Trainer", url="g1")])
    monkeypatch.setattr(pipeline.jobspy_fetcher, "fetch", lambda cfg: [_job(title="Old", age_hours=50, url="l1")])
    monkeypatch.setattr(pipeline.remote_boards, "fetch", lambda boards: [])
    monkeypatch.setattr(pipeline.more_boards, "fetch", lambda cfg, report=None: [])
    from src.email_agent import imap_scanner
    monkeypatch.setattr(imap_scanner, "fetch", lambda hours: ([], []))
    res = pipeline.run(ws)
    assert res == {"found": 2, "kept": 1, "export": res["export"], "emails": 0}
    with db.session(ws.db_path) as c:
        assert db.count_by_status(c) == {"QUALIFIED": 1, "REJECTED_STALE": 1}
        assert c.execute("SELECT COUNT(*) FROM platforms").fetchone()[0] >= 5


@pytest.mark.parametrize("loc,keep", [
    ("Remote", True), ("", True), ("Remote - Kenya", True), ("Remote (EMEA)", True), ("Anywhere", True),
    ("US - Remote", False), ("San Francisco, CA (Remote)", False), ("Remote, United Kingdom", False),
])
def test_remote_location_rules(loc, keep):
    cfg = load_yaml("search_queries.yaml")
    from src.intelligence.job_ranker import check_job
    assert check_job(_job(location=loc, is_remote=True), cfg).keep is keep


def test_weak_fit_dropped():
    out = rank([_job(title="Technical Product Manager, GRC Systems", description="governance risk compliance", url="w")],
               load_profile(), load_yaml("search_queries.yaml"))
    assert out[0]["status"] == "REJECTED_KNOCKOUT" and "weak fit" in out[0]["status_reason"]


def test_remote_board_parsers():
    from src.discovery import remote_boards as rb
    now = datetime.now(timezone.utc)
    iso_naive = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    r = rb.parse_remotive({"jobs": [{"id": 1, "url": "https://r/1", "title": "AI Trainer", "company_name": "R",
                                     "candidate_required_location": "Worldwide", "publication_date": iso_naive,
                                     "description": "<p>LLM</p>"}]})
    assert r[0]["age_hours"] == pytest.approx(3, abs=0.1) and r[0]["is_remote"] and r[0]["location"] == "Worldwide"
    ok = rb.parse_remoteok([{"legal": "x"}, {"id": 2, "position": "Python Developer", "company": "K",
                                             "epoch": int((now - timedelta(hours=5)).timestamp()), "url": "https://ok/2",
                                             "tags": ["python"], "description": "d"}])
    assert len(ok) == 1 and ok[0]["age_hours"] == pytest.approx(5, abs=0.1)
    h = rb.parse_himalayas({"jobs": [{"guid": "g", "applicationLink": "https://h/g", "title": "Data Annotator",
                                      "companyName": "H", "pubDate": int((now - timedelta(hours=30)).timestamp()),
                                      "locationRestrictions": ["Kenya", "Nigeria"]}]})
    assert h[0]["age_hours"] == pytest.approx(30, abs=0.1) and h[0]["location"] == "Kenya, Nigeria"
    jb = rb.parse_jobicy({"jobs": [{"id": 3, "url": "https://j/3", "jobTitle": "Backend Engineer", "companyName": "J",
                                    "jobGeo": "Anywhere", "pubDate": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")}]})
    assert jb[0]["age_hours"] == pytest.approx(1, abs=0.1)
    from email.utils import format_datetime
    rss = f"""<rss><channel><item><title>Acme: Prompt Engineer</title><link>https://w/1</link>
      <region>Anywhere in the World</region><pubDate>{format_datetime(now - timedelta(hours=2))}</pubDate>
      <description>&lt;p&gt;LLM&lt;/p&gt;</description></item></channel></rss>"""
    w = rb.parse_wwr(rss)
    assert w[0]["company"] == "Acme" and w[0]["title"] == "Prompt Engineer" and w[0]["age_hours"] == pytest.approx(2, abs=0.1)


def test_linkedin_stops_after_empty_searches(monkeypatch):
    import pandas as pd
    import sys, types
    calls = []
    fake = types.ModuleType("jobspy")
    fake.scrape_jobs = lambda **kw: calls.append(kw) or pd.DataFrame()
    monkeypatch.setitem(sys.modules, "jobspy", fake)
    from src.discovery import jobspy_fetcher
    cfg = load_yaml("search_queries.yaml")
    cfg["jobspy"]["request_pause_seconds"] = 0
    assert jobspy_fetcher.fetch(cfg) == [] and len(calls) == 3


def test_title_cleanup_and_staff_excluded():
    j = _job(title="Staff Software Engineer - Query Planner &amp; Reflections", url="s1")
    assert "&amp;" not in j["title"]
    out = rank([j], load_profile(), load_yaml("search_queries.yaml"))
    assert out[0]["status"] == "REJECTED_KNOCKOUT"


def test_per_source_age_limit():
    cfg = dict(load_yaml("search_queries.yaml"), max_age_hours_by_source={"greenhouse": 72})
    from src.intelligence.job_ranker import check_job
    assert check_job(_job(source="greenhouse:x", age_hours=50), cfg).keep
    assert not check_job(_job(source="remotive", age_hours=50), cfg).keep
