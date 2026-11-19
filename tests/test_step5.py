"""Step 5: strict verification, company-site tabs, portal accounts with email codes, CDP, secrets."""
import json
from datetime import datetime, timezone

import pytest

from src import database as db
from src.email_agent import otp
from src.secret_store import generate_password, protect, unprotect
from src.workspace import ensure_workspace
from tests.test_step3 import FIX, _browser_ok


def test_secret_store_round_trip_and_password_strength(tmp_path):
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    pw = generate_password()
    assert len(pw) == 20 and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)
    enc = protect(pw, ws)
    assert pw not in enc and unprotect(enc, ws) == pw


@pytest.mark.parametrize("subject,text,html,want", [
    ("Your verification code", "Use 482913 to verify your email.", "", {"code": "482913"}),
    ("Workday account", "Your one-time passcode is 123 456. It expires soon.", "", {"code": "123456"}),
    ("Confirm your email", "Click below", '<a href="https://acme.wd3.myworkdayjobs.com/verify?token=abc">Verify email</a>',
     {"link": "https://acme.wd3.myworkdayjobs.com/verify?token=abc"}),
    ("Order 556677 shipped", "Thanks for your order 556677", "", None),
])
def test_extracts_codes_and_links(subject, text, html, want):
    assert otp.extract({"subject": subject, "text": text, "html": html}, "acme.wd3.myworkdayjobs.com") == want


def test_wait_for_code_polls_until_it_arrives():
    calls = []

    def fetch(since):
        calls.append(1)
        return [] if len(calls) < 2 else [{"from_addr": "no-reply@smartrecruiters.com", "subject": "Your code",
                                           "text": "Verification code: 734512", "html": ""}]
    got = otp.wait_for_code(datetime.now(timezone.utc), "jobs.smartrecruiters.com", timeout=5, poll=0.01, fetch=fetch)
    assert got == {"code": "734512"} and len(calls) == 2
    assert otp.wait_for_code(datetime.now(timezone.utc), "x.com", timeout=0.05, poll=0.01, fetch=lambda s: []) is None


def _pages_route(ctx, pages):
    ctx.route("**/*", lambda r: r.fulfill(body=pages.get(r.request.url.split("?")[0], "<p>404</p>"), content_type="text/html"))


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_registers_portal_account_and_enters_email_code(tmp_path, monkeypatch):
    from src.automation import portal_accounts
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    pages = {
        "https://acme.portal.test/login": "<h1>Sign in</h1><input type=email><input type=password><button>Sign in</button>"
                                          "<a href='https://acme.portal.test/register'>Create an account</a>",
        "https://acme.portal.test/register": "<h1>Create account</h1><form onsubmit=\"event.preventDefault();location='https://acme.portal.test/verify'\">"
            "<label for=e>Email</label><input id=e type=email required><label for=p>Password</label><input id=p type=password required>"
            "<label for=c>Confirm password</label><input id=c type=password required>"
            "<label><input type=checkbox id=t required> I agree to the Terms</label>"
            "<label><input type=checkbox id=m> Send me marketing offers</label><button type=submit>Create account</button></form>",
        "https://acme.portal.test/verify": "<p>We sent a verification code to your email.</p><input name=code>"
            "<button onclick=\"if(document.querySelector('[name=code]').value==='482913')location='https://acme.portal.test/apply'\">Verify</button>",
        "https://acme.portal.test/apply": "<h1>Apply</h1>",
    }
    seen = {}
    with browser(ws) as ctx:
        _pages_route(ctx, pages)
        page = ctx.new_page()
        page.goto("https://acme.portal.test/login")
        page.goto("https://acme.portal.test/register")

        def capture(page_):
            seen["marketing"] = page_.locator("#m").is_checked()
            seen["terms"] = page_.locator("#t").is_checked()
            seen["pw"] = page_.locator("#p").input_value()
        orig = portal_accounts._click_text

        def click(page_, rx):
            if "register" in page_.url:
                capture(page_)
            return orig(page_, rx)
        monkeypatch.setattr(portal_accounts, "_click_text", click)
        res = portal_accounts.sign_in_or_register(
            page, ws, "me@example.com", {"enabled": True, "accept_terms": True, "code_wait_seconds": 1},
            profile={"personal": {"first_name": "D", "last_name": "O", "full_name": "D O"}},
            wait_for_code=lambda since, host, timeout: {"code": "482913"})
        assert res == "ok" and page.url == "https://acme.portal.test/apply"
    assert seen["terms"] and not seen["marketing"] and len(seen["pw"]) == 20
    with db.session(ws.db_path) as c:
        row = c.execute("SELECT * FROM portal_credentials").fetchone()
        assert row["host"] == "acme.portal.test" and row["verified"] == 1 and seen["pw"] not in row["secret"]
        assert portal_accounts.load_credentials(c, ws, "acme.portal.test") == ("me@example.com", seen["pw"])


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_portal_stops_at_captcha_and_without_permission(tmp_path, monkeypatch):
    from src.automation import portal_accounts
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with browser(ws) as ctx:
        page = ctx.new_page()
        page.set_content("<input type=email><input type=password><div class='g-recaptcha' style='width:300px;height:80px'></div><button>Sign up</button>")
        assert portal_accounts.sign_in_or_register(page, ws, "me@x.com", {"enabled": True}, profile={"personal": {}}) == "captcha"
        page.set_content("<h1>Create account</h1><input type=email><input type=password><label><input type=checkbox required> I accept the terms</label><button>Sign up</button>")
        assert portal_accounts.sign_in_or_register(page, ws, "me@x.com", {"enabled": True, "accept_terms": False},
                                                   profile={"personal": {}}) == "needs_you"


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_verify_guard(tmp_path, monkeypatch):
    from src.automation import form_fields
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    cv = tmp_path / "Dickens_CV.docx"
    cv.write_bytes(b"x")
    form = ("<form><label for=a>Name</label><input id=a required><label for=r>Resume</label><input id=r name=resume type=file required>"
            "<label><input type=checkbox id=c required data-jsa=f9> I agree</label></form>")
    with browser(ws) as ctx:
        page = ctx.new_page()
        page.set_content(form)
        probs = form_fields.verify(page, ("f9",))
        assert any("empty" in p for p in probs) and any("no file" in p for p in probs) and not any("f9" in p for p in probs)
        page.fill("#a", "Dickens")
        page.set_input_files("#r", str(cv))
        assert form_fields.verify(page, ("f9",)) == []
        assert any("unanswered" in p for p in form_fields.verify(page))  # consent counted when not excluded
        page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=\"field-error\">This field is required</div>')")
        assert any("error shown" in p for p in form_fields.verify(page, ("f9",)))


@pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")
def test_apply_button_opening_company_tab_is_followed_in_that_tab(tmp_path, monkeypatch):
    from src.automation import form_stager
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    pages = {"https://board.test/job/1": "<h1>Data Engineer</h1><button onclick=\"window.open('https://apply.workable.com/acme/j/123/apply/')\">Apply for this job</button>",
             "https://apply.workable.com/acme/j/123/apply/": "<script>sessionStorage.setItem('s','1')</script><form><label for=a>First name</label><input id=a>"
                 "<label for=b>Last name</label><input id=b><label for=e>Email</label><input id=e type=email><label for=r>Resume</label><input id=r type=file></form>"}
    with browser(ws) as ctx:
        _pages_route(ctx, pages)
        page = ctx.new_page()
        url, active = form_stager._follow(page, {"url": "https://board.test/job/1"})
        assert url == "https://apply.workable.com/acme/j/123/apply/" and active is not page
        assert active.evaluate("sessionStorage.getItem('s')") == "1"  # same tab, session kept


def test_cdp_is_off_for_tests_and_detects_running_chrome(monkeypatch):
    from src.automation import browser_manager as bm
    monkeypatch.setenv("JOBSPRINT_CHROMIUM_PATH", "/x")
    assert bm._use_cdp(False)[0] is False
    monkeypatch.delenv("JOBSPRINT_CHROMIUM_PATH")
    monkeypatch.setattr(bm, "find_chrome", lambda: "chrome.exe")
    ok, port = bm._use_cdp(False)
    assert ok is True and port == 9222
    assert bm._use_cdp(True)[0] is False  # headless test runs never attach to your Chrome
