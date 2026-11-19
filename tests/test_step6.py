"""Step 6: CAPTCHA / SMS hand-off, Google Messages codes, self-update with no downtime."""
import http.client
import json
import subprocess
import sys
import threading
import time

import pytest

from src import database as db
from src.automation import human_assist_interceptor as hia
from src.workspace import ensure_workspace
from tests.test_step3 import _browser_ok

needs_browser = pytest.mark.skipif(not _browser_ok(), reason="Playwright browser not installed")


@pytest.mark.parametrize("text,want", [
    ("G-482913 is your Google verification code.", "482913"),
    ("Your Workday code is 7342. Don't share it.", "7342"),
    ("Use 123 456 to sign in to Greenhouse", "123456"),
    ("SDF45GH Confirmed. Ksh500.00 sent to JOHN 0712345678 on 19/11/26. M-PESA balance is Ksh1,234.00", None),
    ("Your account balance is KES 10,000. Transaction code 998877", None),
    ("See you at 2026 meetup", None),
    ("Hi, are you free tomorrow?", None),
])
def test_only_real_verification_codes_are_taken_from_sms(text, want):
    assert hia.code_from_sms(text) == want


@needs_browser
def test_detects_captcha_sms_and_ignores_invisible_badges(tmp_path, monkeypatch):
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    with browser(ws) as ctx:
        page = ctx.new_page()
        page.set_content("<form><input name=a><div class='g-recaptcha' style='width:300px;height:78px'></div></form>")
        assert hia.detect(page) == "captcha"
        page.evaluate("document.querySelector('form').insertAdjacentHTML('beforeend', '<textarea name=g-recaptcha-response>' + 'x'.repeat(40) + '</textarea>')")
        assert hia.detect(page) is None  # solved
        page.set_content("<form><input name=a><div class='g-recaptcha' data-size=invisible style='width:256px;height:60px'></div></form>")
        assert hia.detect(page) is None
        page.set_content("<p>We sent a text message to your phone ending 21.</p><input name=otp>")
        assert hia.detect(page) == "sms"
        page.set_content("<p>We sent a code to your email. Check your inbox.</p><input name=code>")
        assert hia.detect(page) is None
        page.set_content("<p>Verify you are human by completing the action below.</p>")
        assert hia.detect(page) == "captcha" and hia.blocking_challenge(page)


@needs_browser
def test_reads_new_sms_code_from_google_messages_tab(tmp_path, monkeypatch):
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    msgs = ("<mws-conversation-list-item>Safaricom: Your data bundle 112233 expires</mws-conversation-list-item>"
            "<script>setTimeout(() => document.body.insertAdjacentHTML('afterbegin',"
            "'<mws-conversation-list-item>Acme: 553311 is your verification code</mws-conversation-list-item>'), 700)</script>")
    app = ("<p>Enter the code we sent by SMS to your phone</p><input name=otp>"
           "<button onclick=\"if(document.querySelector('[name=otp]').value==='553311')document.body.innerHTML='<h1>Apply now</h1>'\">Verify</button>")
    with browser(ws) as ctx:
        ctx.route("**/*", lambda r: r.fulfill(body=msgs if "messages.google.com" in r.request.url else app, content_type="text/html"))
        sms = ctx.new_page()
        sms.goto("https://messages.google.com/web/conversations")
        page = ctx.new_page()
        page.goto("https://jobs.acme.test/verify")
        assert hia.detect(page) == "sms"
        assert hia.assist(page, {"company": "Acme"}, ctx=ctx, sms_wait=5) == "auto"
        assert "Apply now" in page.inner_text("body")
        # No Messages tab and nobody watching (hidden browser): give up at once, no waiting.
        sms.close()
        page.goto("https://jobs.acme.test/verify")
        t = time.monotonic()
        assert hia.assist(page, {"company": "Acme"}, ctx=ctx) == "timeout" and time.monotonic() - t < 5


@needs_browser
def test_waits_for_you_to_solve_then_resumes(tmp_path, monkeypatch):
    from src.automation.browser_manager import browser
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    alerts = []
    monkeypatch.setattr(hia, "notify", lambda t, m: alerts.append(m))
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    with browser(ws) as ctx:
        page = ctx.new_page()
        page.set_content("<input id=n value=Dickens><div class='h-captcha' style='width:300px;height:78px'></div>"
                         "<script>setTimeout(() => document.querySelector('.h-captcha').remove(), 1200)</script>")
        assert hia.assist(page, {"company": "Acme Ltd"}, wait_seconds=10, poll=0.3) == "resolved"
        assert page.locator("#n").input_value() == "Dickens"  # nothing lost
        assert alerts == ["[JobSprintAgent] Action needed on Acme Ltd: Solve the CAPTCHA in Chrome"]
        page.set_content("<div class='h-captcha' style='width:300px;height:78px'></div>")
        assert hia.assist(page, {"company": "Acme"}, wait_seconds=0.8, poll=0.3) == "timeout"


@needs_browser
def test_unanswered_challenge_marks_job_needs_manual_otp(tmp_path, monkeypatch):
    from src.automation import form_stager
    from src.automation.browser_manager import browser
    from src.intelligence.answer_engine import AnswerEngine
    from src.cv_tailor.single_template_tailor import load_profile
    monkeypatch.setenv("JOBSPRINT_HEADLESS", "1")
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    db.init_db(ws.db_path)
    with db.session(ws.db_path) as c:
        jid = c.execute("INSERT INTO jobs(source, url, apply_url, title, company, status, first_seen_at, updated_at) VALUES "
                        "('t','https://boards.greenhouse.io/acme/jobs/1','https://boards.greenhouse.io/acme/jobs/1','Data Analyst','Acme','QUALIFIED',?,?)",
                        (db.now_iso(), db.now_iso())).lastrowid
    with browser(ws) as ctx, db.session(ws.db_path) as c:
        ctx.route("**/*", lambda r: r.fulfill(body="<p>Checking your browser. Verify you are human.</p><iframe src='https://challenges.cloudflare.com/x' width=300 height=65></iframe>",
                                              content_type="text/html"))
        job = dict(c.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone())
        state = form_stager.stage_job(ctx.new_page(), c, ws, job, AnswerEngine(load_profile()), load_profile())
        assert state == "NEEDS_MANUAL_OTP"
        assert c.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == "NEEDS_MANUAL_OTP"


# ---------- self-update ----------

def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_updates_from_github_keeping_env_and_backing_up(tmp_path):
    from src import updater
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    origin, app = tmp_path / "origin", tmp_path / "app"
    origin.mkdir(); app.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "run_sprint.py").write_text("v2")
    (origin / "requirements.txt").write_text("")
    (origin / ".env.example").write_text("EMAIL_PASSWORD=")
    _git("add", "-A", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v2", cwd=origin)
    (app / "run_sprint.py").write_text("v1")
    (app / "requirements.txt").write_text("")
    (app / ".env").write_text("EMAIL_PASSWORD=secret")
    (app / updater.MARKER).write_text("old")
    cfg = {"enabled": True, "repo": str(origin), "branch": "main", "run_tests": False}
    res = updater.check_and_update(ws, app_dir=app, cfg=cfg)
    assert res["updated"] and res["error"] is None
    assert (app / "run_sprint.py").read_text() == "v2" and (app / ".env").read_text() == "EMAIL_PASSWORD=secret"
    assert (app / "_backups" / "old" / "run_sprint.py").read_text() == "v1" and not (app / "_backups" / "old" / ".env").exists()
    assert updater.check_and_update(ws, app_dir=app, cfg=cfg)["updated"] is False  # already current
    (ws.data / "sprint.lock").write_text("1")
    (origin / "run_sprint.py").write_text("v3")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "v3", cwd=origin)
    assert "in progress" in updater.check_and_update(ws, app_dir=app, cfg=cfg)["error"]


def test_failing_tests_keep_the_current_version(tmp_path, monkeypatch):
    from src import updater
    ws = ensure_workspace(tmp_path / "JobSprintAgent")
    origin, app = tmp_path / "origin", tmp_path / "app"
    origin.mkdir(); app.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "run_sprint.py").write_text("broken")
    _git("add", "-A", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x", cwd=origin)
    (app / "run_sprint.py").write_text("good")
    monkeypatch.setattr(updater, "run_tests", lambda src, python=None: (False, "1 failed"))
    res = updater.check_and_update(ws, app_dir=app, cfg={"repo": str(origin), "branch": "main", "run_tests": True})
    assert not res["updated"] and "tests failed" in res["error"] and (app / "run_sprint.py").read_text() == "good"


BACKEND = r'''
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
port, tag = int(sys.argv[1]), sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/slow": time.sleep(1.5)
        body = json.dumps({"commit": tag, "path": self.path}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_POST = do_GET
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def test_front_door_swaps_versions_without_dropping_requests(tmp_path):
    from src.web import frontdoor
    script = tmp_path / "backend.py"
    script.write_text(BACKEND)
    tags = iter(["v1", "v2"])
    door = frontdoor.FrontDoor(None, port=0,
                               backend_factory=lambda port: frontdoor.Backend(port, [sys.executable, str(script), str(port), next(tags)]))
    door.current = door.start_backend()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), door.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read())
    try:
        assert get("/api/x") == (200, {"commit": "v1", "path": "/api/x"})
        slow = {}
        t = threading.Thread(target=lambda: slow.update(r=get("/slow")))
        t.start()
        time.sleep(0.3)
        old = door.current
        assert door.swap()
        assert get("/api/x")[1]["commit"] == "v2"  # new requests go to the new version at once
        t.join()
        assert slow["r"] == (200, {"commit": "v1", "path": "/slow"})  # the request in flight finished on the old one
        for _ in range(50):
            if old.proc.poll() is not None:
                break
            time.sleep(0.2)
        assert old.proc.poll() is not None  # and the old one was retired
    finally:
        srv.shutdown()
        door.current.stop(grace=0)
