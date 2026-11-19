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
    ("AfterQuery", "https://experts.afterquery.com/apply"),
    ("Outlier", "https://app.outlier.ai/login"),
    ("Mercor", "https://work.mercor.com/"),
]


@contextmanager
def browser(ws, headless: bool | None = None):
    """Yield a persistent Playwright browser context."""
    from playwright.sync_api import sync_playwright
    if headless is None:
        headless = os.environ.get("JOBSPRINT_HEADLESS", "0") == "1"
    exe = os.environ.get("JOBSPRINT_CHROMIUM_PATH") or None
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(ws.browser_profile), headless=headless, executable_path=exe,
            viewport={"width": 1280, "height": 900}, accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled"])
        ctx.set_default_timeout(15000)
        try:
            yield ctx
        finally:
            ctx.close()


def login_setup(ws) -> None:
    """Open the login pages so you can sign in once; the profile remembers you."""
    with browser(ws, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, (name, url) in enumerate(LOGIN_PAGES):
            p = page if i == 0 else ctx.new_page()
            try:
                p.goto(url, wait_until="domcontentloaded")
            except Exception as exc:  # a slow site must not stop the others
                log.warning("Could not open %s: %s", name, exc)
        print("\nSign in on each tab you use, then come back here and press Enter to save and close.")
        input()
