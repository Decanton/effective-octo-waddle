"""Persistent local Chromium profile so your logins survive between sprints.

The profile lives in Desktop\\JobSprintAgent\\browser_profile. Only one process
can use it at a time, so staging and submitting take turns.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

log = logging.getLogger("jobsprint")

LOGIN_PAGES = [
    ("LinkedIn", "https://www.linkedin.com/login"),
    ("Himalayas", "https://himalayas.app/login"),
    ("Jobicy", "https://jobicy.com/"),
    ("Wellfound", "https://wellfound.com/login"),
    ("AfterQuery", "https://experts.afterquery.com/apply"),
    ("Outlier", "https://app.outlier.ai/login"),
    ("Mercor", "https://work.mercor.com/"),
]


MESSAGES_URL = "https://messages.google.com/web/conversations"


def _keep_messages() -> bool:
    from ..settings import load_settings
    return bool((load_settings().get("human_assist") or {}).get("keep_messages_open", True))


def ensure_messages_tab(ctx):
    """Keep Google Messages for Web open in Job Agent Chrome, so SMS codes can be read. Never closed by the agent."""
    for p in ctx.pages:
        if "messages.google.com" in (p.url or ""):
            return p
    try:
        p = ctx.new_page()
        p.goto(MESSAGES_URL, wait_until="domcontentloaded", timeout=20000)
        return p
    except Exception as exc:
        log.info("Could not open Google Messages: %s", exc)
        return None


def find_chrome() -> str | None:
    """Your installed Google Chrome, if any. Google sign-in works there; it is often refused in test browsers."""
    import shutil
    from pathlib import Path
    if os.environ.get("JOBSPRINT_NO_CHROME") == "1":
        return None
    cands = [Path(os.environ.get(v, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
             for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if os.environ.get(v)]
    cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def _copied_profile(ws):
    from .chrome_profile import cloned
    try:
        return cloned(ws)
    except Exception:
        return None


def profile_dir(ws):
    """One saved profile for every run, so sign-ins you make once are reused."""
    return ws.browser_profile


def _cdp_up(port: int) -> bool:
    import requests
    try:
        return requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1.5).ok
    except Exception:
        return False


def start_debug_chrome(ws, port: int, urls: list[str] | None = None) -> bool:
    """Start Job Agent Chrome with a local debugging port (only this laptop can connect)."""
    import subprocess
    import time
    chrome = find_chrome()
    if not chrome:
        return False
    if _cdp_up(port):
        return True
    if _keep_messages():
        urls = [MESSAGES_URL, *[u for u in (urls or []) if u != MESSAGES_URL]]
    subprocess.Popen([chrome, *chrome_args(ws), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                      "--no-first-run", "--no-default-browser-check", *(urls or [])])
    for _ in range(40):
        time.sleep(0.5)
        if _cdp_up(port):
            return True
    log.warning("Job Agent Chrome didn't open its debugging port. If a Job Agent Chrome window is already open "
                "(started without the shortcut), close it and run again.")
    return False


def _use_cdp(headless: bool) -> tuple[bool, int]:
    from ..settings import load_settings
    cfg = load_settings().get("browser") or {}
    port = int(cfg.get("cdp_port", 9222))
    ok = bool(cfg.get("use_cdp")) and not headless and not os.environ.get("JOBSPRINT_CHROMIUM_PATH") \
        and os.environ.get("JOBSPRINT_NO_CDP") != "1" and find_chrome() is not None
    return ok, port


@contextmanager
def browser(ws, headless: bool | None = None):
    """Yield a browser context.

    Normally this is your Job Agent Chrome, reached over the local debugging port, so its
    Google sign-in, cookies and tabs are shared with the agent. If that isn't possible it
    falls back to launching Chrome (or the bundled Chromium) on the same profile.
    """
    from playwright.sync_api import sync_playwright
    if headless is None:
        headless = os.environ.get("JOBSPRINT_HEADLESS", "0") == "1"
    exe = os.environ.get("JOBSPRINT_CHROMIUM_PATH") or None
    chrome = None if exe else find_chrome()
    cdp, port = _use_cdp(headless)
    with sync_playwright() as pw:
        if cdp and start_debug_chrome(ws, port):
            b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = b.contexts[0] if b.contexts else b.new_context()
            ctx.set_default_timeout(15000)
            if _keep_messages():
                ensure_messages_tab(ctx)  # counted as yours below, so it stays open
            before = set(ctx.pages)
            ctx.new_page()  # the agent works in its own tab
            try:
                yield ctx
            finally:
                for p in ctx.pages:  # close the tabs this run opened; leave your tabs and Chrome running
                    if p not in before and not p.is_closed():
                        try:
                            p.close()
                        except Exception:
                            pass
            return
        data_dir, extra = profile_dir(ws), []
        copy = _copied_profile(ws) if chrome else None
        if copy:  # your own profile, copied with --use-chrome-profile
            data_dir, extra = copy[0], [f"--profile-directory={copy[1]}"]
        ctx = pw.chromium.launch_persistent_context(
            str(data_dir), headless=headless, executable_path=exe or chrome,
            viewport={"width": 1280, "height": 900}, accept_downloads=False,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled", *extra])
        ctx.set_default_timeout(15000)
        if chrome and not headless and _keep_messages():
            ensure_messages_tab(ctx)
            ctx.new_page()  # the agent's own tab stays last
        try:
            yield ctx
        finally:
            ctx.close()


def chrome_args(ws) -> list[str]:
    copy = _copied_profile(ws)
    return [f"--user-data-dir={copy[0]}", f"--profile-directory={copy[1]}"] if copy else [f"--user-data-dir={profile_dir(ws)}"]


def open_chrome(ws, urls: list[str] | None = None) -> None:
    """Open the agent's Chrome profile as a normal window and return right away."""
    import subprocess
    chrome = find_chrome()
    if not chrome:
        print("Google Chrome was not found.")
        return
    cdp, port = _use_cdp(False)
    if cdp:
        if _cdp_up(port):
            subprocess.Popen([chrome, *chrome_args(ws), *(urls or ["about:newtab"])])  # new window in the running one
        else:
            start_debug_chrome(ws, port, urls)
        return
    subprocess.Popen([chrome, *chrome_args(ws), "--no-first-run", "--no-default-browser-check", *(urls or [])])


def pages_to_sign_in(ws) -> list[tuple[str, str]]:
    """The usual sites, plus every site where a job was waiting for a sign-in."""
    from urllib.parse import urlparse

    from .. import database as db
    pages, hosts = list(LOGIN_PAGES), set()
    try:
        with db.session(ws.db_path) as conn:
            rows = conn.execute("SELECT form_url FROM staged_forms WHERE state = 'NEEDS_LOGIN' ORDER BY id DESC").fetchall()
    except Exception:
        rows = []
    for (u,) in rows:
        host = urlparse(u or "").netloc or u
        if host and host not in hosts:
            hosts.add(host)
            pages.append((host, u))
    return pages[:20]


def login_setup(ws) -> None:
    """Open the sign-in pages so you can sign in once; the profile remembers you.

    The first tab is Google: sign in there with your job-search Gmail, and sites
    with "Continue with Google" then work for the agent too. You type your own
    password (or use your phone prompt). The agent never sees or stores it.
    When Chrome is installed it opens as a normal Chrome window (not automated),
    because Google refuses sign-in in automated browsers.
    """
    import subprocess
    account = os.environ.get("EMAIL_ADDRESS", "")
    pages = [("Google", "https://accounts.google.com/AccountChooser?Email=" + account)] + pages_to_sign_in(ws)
    chrome = None if os.environ.get("JOBSPRINT_CHROMIUM_PATH") else find_chrome()
    msg = (f"\nA browser window is open. On the first tab, sign in to Google as {account or 'your job-search Gmail'}.\n"
           "Then on the other tabs, use 'Continue with Google' (or sign in / sign up) on each site you use.\n")
    cdp, _port = _use_cdp(False)
    if chrome and cdp:
        open_chrome(ws, [u for _, u in pages])
        print(msg + "When you're done, come back here and press Enter. (You can leave Job Agent Chrome open.)")
        input()
    elif chrome:
        proc = subprocess.Popen([chrome, *chrome_args(ws), "--no-first-run",
                                 "--no-default-browser-check", *[u for _, u in pages]])
        print(msg + "When you're done, close that Chrome window. The sign-ins are saved for the agent.")
        proc.wait()
    else:
        with browser(ws, headless=False) as ctx:
            page = ctx.pages[-1] if ctx.pages else ctx.new_page()  # the agent's own tab
            for i, (name, url) in enumerate(pages):
                p = page if i == 0 else ctx.new_page()
                try:
                    p.goto(url, wait_until="domcontentloaded")
                except Exception as exc:  # a slow site must not stop the others
                    log.warning("Could not open %s: %s", name, exc)
            print(msg + "When you're done, come back here and press Enter to save and close.")
            input()
    print("Saved. Run START_SPRINT.bat --restage --stage-only (or the daily run) and jobs that needed a sign-in are filled.")
