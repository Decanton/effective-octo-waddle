"""Uses "Continue with Google" on job sites that want an account.

It works only after you have signed in to Google once in the agent's browser
(START_SPRINT.bat --login-setup). The agent then clicks the site's Google button
and picks your job-search account (EMAIL_ADDRESS). It never types a password: if
Google asks for one, or for a phone prompt, it stops and the job waits for you.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("jobsprint")

GOOGLE_BTN = re.compile(r"(continue|sign ?in|log ?in|sign ?up|join|register|apply)\s+(with|using|via)\s+google|^google$", re.I)
GOOGLE_HOST = "accounts.google.com"
CONTINUE_BTN = re.compile(r"^(continue|allow|next|confirm|i agree)$", re.I)


def _google_button(page):
    for el in page.locator("button, a, [role=button], div[tabindex]").all()[:400]:
        try:
            if not el.is_visible():
                continue
            text = " ".join(((el.inner_text(timeout=300) or "") + " " + (el.get_attribute("aria-label") or "") + " " +
                             (el.get_attribute("title") or "")).split())
        except Exception:
            continue
        if text and GOOGLE_BTN.search(text):
            return el
    # Google's own sign-in button lives in an iframe from accounts.google.com.
    for fr in page.frames:
        if GOOGLE_HOST in (fr.url or "") and "gsi/button" in fr.url:
            try:
                btn = fr.locator("[role=button]").first
                if btn.count():
                    return btn
            except Exception:
                pass
    return None


def _finish_google(gpage, account: str, allow_consent: bool, timeout_ms: int = 25000) -> str:
    """Drive Google's chooser in gpage. Returns 'ok', 'needs_password' or 'failed'."""
    waited = 0
    while waited < timeout_ms:
        try:
            if gpage.is_closed():
                return "ok"
            url = gpage.url
        except Exception:
            return "ok"
        if GOOGLE_HOST not in url:
            return "ok"
        try:
            if gpage.locator("input[type=password]:visible").count() or re.search(r"/challenge/", url):
                return "needs_password"
            if gpage.locator("input[type=email]:visible").count():
                return "needs_password"  # not signed in to Google in this browser yet
            acct = gpage.locator(f"[data-email='{account}' i], [data-identifier='{account}' i]").first
            if account and acct.count() and acct.is_visible():
                acct.click()
            else:
                clicked = False
                if allow_consent:
                    for b in gpage.locator("button:visible, [role=button]:visible").all()[:30]:
                        if CONTINUE_BTN.search(" ".join((b.inner_text(timeout=300) or "").split())):
                            b.click()
                            clicked = True
                            break
                if not clicked and account and gpage.get_by_text(account, exact=False).count():
                    gpage.get_by_text(account, exact=False).first.click()
        except Exception:
            pass
        gpage.wait_for_timeout(1500)
        waited += 1500
    return "failed"


def try_google(page, account: str, allow_consent: bool = True) -> str:
    """'ok' when the site is now signed in via Google, 'no_button', 'needs_password' or 'failed'."""
    btn = _google_button(page)
    if btn is None:
        return "no_button"
    ctx = page.context
    before = len(ctx.pages)
    try:
        btn.click()
    except Exception:
        return "failed"
    page.wait_for_timeout(2500)
    gpage = next((p for p in ctx.pages[before:] if not p.is_closed()), None) or page
    result = _finish_google(gpage, account, allow_consent)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(2500)
    if result == "ok":
        log.info("Signed in with Google (%s) at %s", account, page.url.split("/")[2] if "//" in page.url else page.url)
    return result
