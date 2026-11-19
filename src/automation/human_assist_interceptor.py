"""Hand CAPTCHAs and phone (SMS) codes to you, then carry on.

The agent never solves a CAPTCHA. When one blocks the way, or a site texts a code
to your phone, this module:

1. Reads the code itself if Google Messages for Web is open in Job Agent Chrome
   (messages.google.com/web, paired with your phone). Only a NEW message that looks
   like a verification code is used; money messages (M-PESA, bank, balances) are ignored.
2. Otherwise brings that exact tab to the front, shows a desktop alert with a chime
   ("[JobSprintAgent] Action needed on <Company>: ..."), and checks every 2 seconds
   for up to 2 minutes until you've solved it. Then the agent continues on the same
   page, so nothing you or it typed is lost.
3. If nobody acts in time, the job is marked NEEDS_MANUAL_OTP and the run moves on.

Free: no API, no paid solver. Everything runs on this laptop.
"""
from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

log = logging.getLogger("jobsprint")

CHALLENGE_TEXT = re.compile(r"verify (that )?you are (a )?human|are you a robot|checking (if the site connection is secure|your browser)"
                            r"|complete the (security )?check|i'?m not a robot|press (and|&) hold", re.I)
TOKEN_SEL = "textarea[name='g-recaptcha-response'], textarea[name='h-captcha-response'], input[name='cf-turnstile-response']"
SMS_TEXT = re.compile(r"\b(sms|text message|texted|sent (a|the) (code|message) to (your )?(phone|mobile|\+?\d)|"
                      r"phone (verification|code)|verify your (phone|mobile)|two[- ]factor|2fa|mobile number ending)", re.I)
EMAIL_TEXT = re.compile(r"\b(sent|emailed) (a |the |an )?(code|link|verification)\b.{0,40}\b(e-?mail|inbox)|check your (e-?mail|inbox)", re.I)
OTP_INPUT = ("input[autocomplete=one-time-code], input[name*=otp i], input[id*=otp i], input[name*=code i], "
             "input[id*=code i], input[placeholder*=code i], input[aria-label*=code i], input[maxlength='1'][inputmode=numeric]")
CODE_IN_SMS = re.compile(r"(?<!\d)(?<!\d[.,])(\d{4,8}|\d{3}[ -]\d{3})(?!\d|[.,]\d)")
MONEY_SMS = re.compile(r"m-?pesa|\bksh\b|\bkes\b|balance|transaction|confirmed\.?\s+\w*\s*ksh|paid to|received ksh|"
                       r"withdraw|loan|fuliza|bank|account no|\bpin\b", re.I)
CODE_WORDS = re.compile(r"\b(code|otp|verif|passcode|one[- ]time|security|confirm|login|sign[- ]?in)", re.I)
MESSAGES_HOST = "messages.google.com"
# Visible challenges only: the invisible reCAPTCHA v3 badge needs nothing from you.
CHALLENGE_SEL = ("iframe[src*='recaptcha']:not([src*='size=invisible']), iframe[src*='hcaptcha']:not([src*='size=invisible']), "
                 "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile'], .g-recaptcha:not([data-size=invisible]), "
                 ".h-captcha:not([data-size=invisible]), .cf-turnstile")


def _cfg() -> dict:
    from ..settings import load_settings
    return load_settings().get("human_assist") or {}


def _vis_count(page, sel: str) -> int:
    try:
        return sum(1 for el in page.locator(sel).all()[:20] if el.is_visible())
    except Exception:
        return 0


def _text(page) -> str:
    try:
        return page.inner_text("body", timeout=3000) or ""
    except Exception:
        return ""


def captcha_solved(page) -> bool:
    """A widget's answer token is filled in (reCAPTCHA / hCaptcha / Turnstile)."""
    try:
        return bool(page.evaluate(f"""() => [...document.querySelectorAll("{TOKEN_SEL}")].some(e => (e.value || '').length > 20)"""))
    except Exception:
        return False


def detect(page) -> str | None:
    """'captcha' for a visible challenge not yet solved, 'sms' for a phone-code prompt, else None."""
    text = _text(page)
    if (_vis_count(page, CHALLENGE_SEL) or (CHALLENGE_TEXT.search(text) and len(text) < 3000)) and not captcha_solved(page):
        return "captcha"
    if _vis_count(page, OTP_INPUT) and SMS_TEXT.search(text) and not EMAIL_TEXT.search(text):
        return "sms"
    return None


def blocking_challenge(page) -> bool:
    """A challenge page standing in front of the site (e.g. Cloudflare's check), not a CAPTCHA inside a form."""
    from .form_fields import discover
    if detect(page) != "captcha":
        return False
    try:
        return len(discover(page)) <= 2
    except Exception:
        return True


# ---------- Google Messages for Web ----------

def messages_tab(ctx):
    for p in getattr(ctx, "pages", []) or []:
        try:
            if MESSAGES_HOST in (p.url or "") and "/web" in p.url and not p.is_closed():
                return p
        except Exception:
            continue
    return None


def _latest_sms(tab) -> str:
    """Text of the newest conversation in the list (sender + snippet). Reads only; never clicks."""
    for sel in ("mws-conversation-list-item", "[data-e2e-conversation-list-item]", "mws-conversations-list a"):
        try:
            loc = tab.locator(sel)
            if loc.count():
                return " ".join((loc.first.inner_text(timeout=2000) or "").split())
        except Exception:
            continue
    return ""


def code_from_sms(text: str, hints: list[str] | None = None) -> str | None:
    """The verification code in a text message, or None for anything that isn't one."""
    if not text or MONEY_SMS.search(text):
        return None
    low = text.lower()
    if not (CODE_WORDS.search(text) or any(h and h.lower() in low for h in (hints or []))):
        return None
    for m in CODE_IN_SMS.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if 4 <= len(digits) <= 8 and not re.fullmatch(r"(19|20)\d\d", digits):  # a year is not a code
            return digits
    return None


class SmsWatcher:
    """Remember the newest message when the code is requested, then wait for a different one."""

    def __init__(self, ctx, hints: list[str] | None = None):
        self.tab = messages_tab(ctx) if ctx is not None else None
        self.hints = [h for h in (hints or []) if h and len(h) > 2]
        self.before = _latest_sms(self.tab) if self.tab else ""

    @property
    def available(self) -> bool:
        return self.tab is not None

    def poll(self) -> str | None:
        if not self.tab:
            return None
        now = _latest_sms(self.tab)
        if now and now != self.before:
            code = code_from_sms(now, self.hints)
            if code:
                return code
            self.before = now  # a different (non-code) message arrived; keep waiting
        return None

    def wait(self, seconds: float, poll: float = 1.0) -> str | None:
        end = time.monotonic() + seconds
        while True:
            code = self.poll()
            if code or time.monotonic() >= end:
                return code
            time.sleep(poll)


def enter_code(page, code: str) -> bool:
    from .portal_accounts import _enter_code
    return _enter_code(page, code)


# ---------- telling you ----------

def notify(title: str, message: str) -> None:
    """Desktop alert + chime. Never raises."""
    if os.environ.get("JOBSPRINT_NO_NOTIFY") == "1":
        log.info("ALERT: %s", message)
        return
    print("\a" + message, flush=True)
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass
    try:
        from plyer import notification  # optional
        notification.notify(title=title, message=message[:250], app_name="JobSprintAgent", timeout=20)
        return
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
                  "$n.ShowBalloonTip(20000, $env:JSA_T, $env:JSA_M, 'Warning'); Start-Sleep 20; $n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                             env={**os.environ, "JSA_T": title, "JSA_M": message[:250]},
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", f'display notification "{message[:200]}" with title "{title}" sound name "Glass"'])
        else:
            subprocess.Popen(["notify-send", "-u", "critical", title, message[:250]])
    except Exception:
        pass


def _front(page) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass


# ---------- the main entry ----------

def assist(page, job: dict | None = None, ctx=None, kind: str | None = None, wait_seconds: float | None = None,
           poll: float = 2.0, sms_wait: float | None = None) -> str:
    """Clear whatever is blocking this page.

    Returns 'clear' (nothing to do), 'auto' (code read from Google Messages and entered),
    'resolved' (you solved it) or 'timeout' (mark the job NEEDS_MANUAL_OTP).
    """
    cfg = _cfg()
    if cfg.get("enabled") is False:
        return "clear" if not (kind or detect(page)) else "timeout"
    kind = kind or detect(page)
    if not kind:
        return "clear"
    ctx = ctx or getattr(page, "context", None)
    company = (job or {}).get("company") or urlparse(page.url or "").netloc or "a job site"
    host = urlparse(page.url or "").netloc.replace("www.", "")
    hints = [company, host.split(".")[0] if host else ""]
    if wait_seconds is None:  # nobody can solve anything in a hidden (headless) browser
        wait_seconds = 0 if os.environ.get("JOBSPRINT_HEADLESS") == "1" else cfg.get("wait_seconds", 120)
    wait_seconds = float(wait_seconds)
    sms_wait = float(cfg.get("sms_wait_seconds", 10) if sms_wait is None else sms_wait)

    watcher = SmsWatcher(ctx, hints) if kind == "sms" else None
    if watcher and watcher.available:
        log.info("  waiting for the SMS code from %s in Google Messages", company)
        code = watcher.wait(sms_wait, poll=min(poll, 1.0))
        if code and enter_code(page, code):
            page.wait_for_timeout(2500)
            if detect(page) != "sms":
                log.info("  entered the SMS code from Google Messages")
                return "auto"

    what = "Solve the CAPTCHA" if kind == "captcha" else "Enter the SMS code"
    msg = f"[JobSprintAgent] Action needed on {company}: {what} in Chrome"
    if wait_seconds <= 0:
        log.info("  %s: no one to ask in a hidden browser", msg)
        return "timeout"
    _front(page)
    notify("JobSprintAgent: action needed", msg)
    log.info("  %s (waiting up to %ds)", msg, int(wait_seconds))
    end = time.monotonic() + wait_seconds
    while True:
        try:
            page.wait_for_timeout(int(poll * 1000))
        except Exception:
            return "timeout"  # the tab was closed
        if watcher and watcher.available:
            code = watcher.poll()
            if code:
                enter_code(page, code)
                page.wait_for_timeout(2000)
        if detect(page) is None:
            log.info("  thanks, carrying on")
            return "resolved"
        if time.monotonic() >= end:
            break
    log.info("  nobody solved it in time; the job is kept for you as NEEDS_MANUAL_OTP")
    return "timeout"


def pause(kind: str = "step") -> None:
    """A short, steady pause between form sections so sites aren't hammered. Off in tests."""
    if os.environ.get("JOBSPRINT_HEADLESS") == "1" or os.environ.get("JOBSPRINT_FAST") == "1":
        return
    cfg = (_cfg().get("pacing") or {})
    lo, hi = cfg.get("between_steps_seconds", [1.5, 4.0]) if kind == "step" else cfg.get("between_fields_seconds", [0.2, 0.6])
    time.sleep(random.uniform(float(lo), float(hi)))
