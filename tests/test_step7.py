"""Step 7: Kenyan job sites and blogs, email-your-CV posts, unattended CAPTCHA skipping."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from src.automation import email_apply
from src.discovery import more_boards as mb
from src.discovery.common import html_to_text


def test_cloudflare_hidden_emails_are_decoded():
    key, addr = 0x42, "hr@acme.co.ke"
    hexs = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in addr)
    html = f'<p>Send your CV and cover letter to <a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="{hexs}">[email&#160;protected]</a></p>'
    assert addr in html_to_text(html)
    assert email_apply.find_apply_email(html_to_text(html))["to"] == addr


@pytest.mark.parametrize("raw,title,company", [
    ("Data Analyst at Safaricom PLC", "Data Analyst", "Safaricom PLC"),
    ("KCB Bank Jobs: ICT Officer", "ICT Officer", "KCB Bank"),
    ("Mechanical Engineer Job at Bamburi Cement - Mombasa", "Mechanical Engineer", "Bamburi Cement"),
    ("Software Developer", "Software Developer", None),
])
def test_split_title(raw, title, company):
    assert mb.split_title(raw) == (title, company)


def test_rss_feed_with_email_application():
    now = datetime.now(timezone.utc)
    rss = f"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
    <item><title>Data Analyst at Acme Kenya</title><link>https://www.jobwebkenya.com/jobs/data-analyst-acme/</link>
      <pubDate>{format_datetime(now - timedelta(hours=3))}</pubDate>
      <content:encoded><![CDATA[<p>Location: Nairobi</p><p>Requirements: SQL, Python, Power BI.</p>
      <h3>How to Apply</h3><p>Send your CV and cover letter to recruitment@acme.co.ke with the subject "Data Analyst" by 30th November.</p>]]></content:encoded></item>
    <item><title>Old job at X</title><link>https://www.jobwebkenya.com/jobs/old/</link><pubDate>{format_datetime(now - timedelta(days=3))}</pubDate><description>x</description></item>
    </channel></rss>"""
    jobs = mb.parse_rss(rss, "jobwebkenya")
    j = jobs[0]
    assert (j["title"], j["company"], j["location"]) == ("Data Analyst", "Acme Kenya", "Nairobi")
    assert 2.5 < j["age_hours"] < 3.5 and jobs[1]["age_hours"] > 24
    found = email_apply.find_apply_email(j["description"])
    assert found == {"to": "recruitment@acme.co.ke", "subject_hint": "Data Analyst"}


def test_blog_own_address_is_never_used():
    assert email_apply.find_apply_email("Send your CV to info@jobwebkenya.com to get job alerts") is None
    assert email_apply.find_apply_email("Interested candidates should send their CV and cover letter to info@acmeltd.co.ke")["to"] == "info@acmeltd.co.ke"


def test_jobposting_page_and_dates():
    today = datetime.now(mb.NAIROBI).date().isoformat()
    html = """<html><script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebPage"},
      {"@type":"JobPosting","title":"Backend Developer","datePosted":"%s","hiringOrganization":{"@type":"Organization","name":"Twiga Foods"},
       "jobLocation":{"@type":"Place","address":{"addressLocality":"Nairobi","addressCountry":"KE"}},
       "description":"&lt;p&gt;Python, Django, REST&lt;/p&gt;"}]}</script><body><h2>How to apply</h2><p>Email your CV to careers@twiga.com</p></body></html>""" % today
    j = mb.job_from_page("https://www.brightermonday.co.ke/listings/backend-developer-x", html, "brightermonday")
    assert (j["title"], j["company"], j["location"]) == ("Backend Developer", "Twiga Foods", "Nairobi, KE")
    assert j["age_hours"] <= 24 and "Django" in j["description"] and "careers@twiga.com" in j["description"]
    yday = (datetime.now(mb.NAIROBI) - timedelta(days=1)).date().isoformat()
    assert mb.age_of(yday) > 24


def test_html_board_reads_listing_then_pages():
    today = datetime.now(mb.NAIROBI).date().isoformat()
    page = lambda t: f'<script type="application/ld+json">{{"@type":"JobPosting","title":"{t}","datePosted":"{today}","hiringOrganization":{{"name":"Co"}},"description":"x"}}</script>'

    class R:
        def __init__(self, text): self.text, self.ok = text, True
        def raise_for_status(self): pass

    class S:
        def get(self, url, timeout=None):
            if url.endswith("/jobs"):
                return R('<a href="/listings/a-1">A</a><a href="/listings/b-2?x=1">B</a><a href="/about">x</a><a href="/listings/a-1">A</a>')
            return R(page("Job " + url.rsplit("/", 1)[-1]))
    jobs = mb.fetch_html_board(S(), {"name": "bm", "list_url": "https://bm.test/jobs", "link": r"/listings/[^\"'?#]+"}, pause=0)
    assert [j["title"] for j in jobs] == ["Job a-1", "Job b-2"]


def test_a_failing_source_never_stops_the_others(monkeypatch):
    class S:
        headers = {}
        def get(self, url, timeout=None):
            raise OSError("blocked")
    monkeypatch.setattr(mb, "_session", lambda: S())
    report = {}
    assert mb.fetch({"more_sources": {"rss_feeds": [{"name": "a", "url": "u"}], "html_boards": [], "json_apis": ["arbeitnow"]}}, report) == []
    assert report["a"][2] and report["arbeitnow"][2]


def test_unattended_skips_captcha_at_once(monkeypatch, tmp_path):
    from src.automation import human_assist_interceptor as hia
    from tests.test_step3 import _browser_ok
    if not _browser_ok():
        pytest.skip("no browser")
    from src.automation.browser_manager import browser
    from src.workspace import ensure_workspace
    import time
    monkeypatch.delenv("JOBSPRINT_HEADLESS", raising=False)
    alerts = []
    monkeypatch.setattr(hia, "notify", lambda t, m: alerts.append(m))
    monkeypatch.setattr(hia, "_cfg", lambda: {"attended": False, "wait_seconds": 120})
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    with browser(ws, headless=True) as ctx:
        page = ctx.new_page()
        page.set_content("<div class='g-recaptcha' style='width:300px;height:78px'></div>")
        t = time.monotonic()
        assert hia.assist(page, {"company": "Acme"}) == "timeout"
        assert time.monotonic() - t < 3 and alerts == []  # no chime, no waiting


def test_stage_order_puts_email_posts_and_ats_first():
    from src.automation import form_stager
    jobs = [{"url": "https://weworkremotely.com/x", "description": ""},
            {"url": "https://boards.greenhouse.io/acme/jobs/1", "description": ""},
            {"url": "https://jobwebkenya.com/j", "description": "Send your CV and cover letter to hr@acme.co.ke"}]
    assert form_stager.application_url(jobs[1]) and not form_stager.application_url(jobs[0])
