"""Signs in to, or creates, an account on a company's application site.

Used only when "Continue with Google" isn't offered or didn't work. It:
  * signs in with the saved account for that site, if there is one;
  * otherwise registers with EMAIL_ADDRESS and a strong random password, which is saved
    encrypted for your Windows user (table portal_credentials) BEFORE the form is sent;
  * reads the 6-digit code or activation link from your inbox and finishes verification.
It stops (and the job waits for you) at a CAPTCHA, a phone/SMS check, or anything it
can't recognise. Your Google or Gmail password is never used here.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from .. import database as db
from ..secret_store import generate_password, protect, unprotect

log = logging.getLogger("jobsprint")

SIGNUP_WORDS = re.compile(r"create (an |your )?account|sign ?up|register|join now|new user|get started|confirm password|"
                          r"re-?enter password|verify password", re.I)
LOGIN_WORDS = re.compile(r"sign ?in|log ?in|forgot password|returning user|existing user", re.I)
TO_SIGNUP = re.compile(r"^(create (an |your )?account|sign ?up|register|new user\??|join( now)?|don.?t have an account\??.*)$", re.I)
SUBMIT_WORDS = re.compile(r"^(create (an |my |your )?account|sign ?up|register|continue|next|submit|sign ?in|log ?in|verify|confirm)$", re.I)
TERMS = re.compile(r"terms|privacy|agree|consent|accept|policy", re.I)
MARKETING = re.compile(r"newsletter|marketing|promotion|offers|updates by email|job alerts", re.I)
PHONE_CHECK = re.compile(r"(text|sms) (message|code)|phone verification|verify your (phone|mobile)", re.I)
CODE_PAGE = re.compile(r"(verification|confirmation|security|one.?time) code|enter (the )?code|check your (e-?mail|inbox)|"
                       r"we (sent|emailed)|verify your e-?mail|activate your account|confirm your e-?mail|"
                       r"activation (e-?mail|link) (has been |was )?sent", re.I)
LOGIN_URL = re.compile(r"/(auth/)?(log-?in|sign-?in|signin|login)(\b|/|\?|$)|/site/login", re.I)


def login_id(email: str) -> str:
    """The username used where a site asks for a username rather than an email (same every time)."""
    return re.sub(r"[^a-z0-9_]", "", email.split("@")[0].lower())[:30] or "applicant"


def still_on_login(page) -> bool:
    """True when the page is (still) a sign-in form: the sign-in didn't take."""
    page.wait_for_timeout(2500)
    if not page.locator("input[type=password]:visible").count():
        return False
    return bool(LOGIN_URL.search(page.url or "") or (LOGIN_WORDS.search(_text(page)) and not SIGNUP_WORDS.search(_text(page))))


def _host(page) -> str:
    return urlparse(page.url).netloc.lower().replace("www.", "")


def _visible(page, sel):
    return [el for el in page.locator(sel).all() if _try(el.is_visible)]


def _try(fn, default=False):
    try:
        return fn()
    except Exception:
        return default


def _text(page) -> str:
    return _try(lambda: page.inner_text("body", timeout=3000), "") or ""


def _click_text(page, rx) -> bool:
    for el in _visible(page, "button, a, input[type=submit], [role=button]")[:200]:
        label = " ".join(((_try(lambda: el.inner_text(timeout=300), "") or "") + " " + (el.get_attribute("value") or "")).split())
        if label and rx.search(label) and len(label) < 60:
            if _try(lambda: el.click() or True):
                page.wait_for_timeout(2500)
                return True
    return False


def _label_for(page, el) -> str:
    return _try(lambda: el.evaluate(
        "e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.placeholder || e.name || e.id || '')"), "") or ""


def _fill_identity(page, email: str, password: str, profile: dict) -> None:
    per = profile.get("personal", {})
    for el in _visible(page, "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=file])"):
        label = _label_for(page, el).lower()
        kind = (el.get_attribute("type") or "text").lower()
        value = None
        if kind == "password":
            value = password
        elif kind == "email" or re.search(r"e-?mail", label):
            value = email
        elif re.search(r"username|user name|user ?id|login", label):  # a username, not an email: some sites refuse "@"
            value = login_id(email)
        elif re.search(r"first|given", label):
            value = per.get("first_name")
        elif re.search(r"last|family|surname", label):
            value = per.get("last_name")
        elif re.search(r"full name|^name$|your name", label):
            value = per.get("full_name")
        elif kind == "tel" or re.search(r"phone|mobile", label):
            value = per.get("phone")
        elif re.search(r"country", label):
            value = per.get("country")
        elif re.search(r"city|location", label):
            value = per.get("city")
        if value and not _try(el.input_value, ""):
            _try(lambda: el.fill(str(value)))


def _tick_terms(page, accept: bool) -> bool:
    """Tick required terms/privacy boxes when allowed; never tick marketing ones. False if a required box is left."""
    left = False
    for box in _visible(page, "input[type=checkbox]"):
        label = _label_for(page, box)
        required = box.get_attribute("required") is not None or box.get_attribute("aria-required") == "true"
        if MARKETING.search(label):
            continue
        if TERMS.search(label) or required:
            if accept:
                _try(lambda: box.check(force=True))
            elif required:
                left = True
    return not left


def _captcha(page) -> bool:
    from .form_fields import has_captcha
    return has_captcha(page)


def _enter_code(page, code: str) -> bool:
    boxes = _visible(page, "input[autocomplete=one-time-code], input[name*=code i], input[id*=code i], input[name*=otp i], "
                           "input[inputmode=numeric], input[maxlength='1']")
    if not boxes:
        boxes = [el for el in _visible(page, "input[type=text], input[type=tel], input[type=number]") if not _try(el.input_value, "")]
    if not boxes:
        return False
    if len(boxes) >= len(code) and all((b.get_attribute("maxlength") or "") == "1" for b in boxes[:len(code)]):
        for b, ch in zip(boxes, code):
            _try(lambda: b.fill(ch))
    else:
        _try(lambda: boxes[0].fill(code))
    _click_text(page, SUBMIT_WORDS)
    return True


def save_credentials(conn, ws, host: str, email: str, password: str, verified: bool = False) -> None:
    conn.execute("INSERT INTO portal_credentials(host, email, secret, verified, created_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(host) DO UPDATE SET email = excluded.email, secret = excluded.secret, verified = excluded.verified",
                 (host, email, protect(password, ws), int(verified), db.now_iso()))


def load_credentials(conn, ws, host: str) -> tuple[str, str] | None:
    row = conn.execute("SELECT email, secret FROM portal_credentials WHERE host = ?", (host,)).fetchone()
    if not row:
        return None
    try:
        return row["email"], unprotect(row["secret"], ws)
    except Exception:
        return None


def _human(page, kind: str) -> bool:
    """Ask you (or Google Messages) for a CAPTCHA or SMS code. True when it was cleared."""
    from . import human_assist_interceptor as hia
    return hia.assist(page, None) != "timeout"  # detects the kind itself; an invisible badge needs nothing


def _sign_in(page, host: str, creds: tuple[str, str], profile: dict, cfg: dict, since, wait_for_code) -> str:
    """Sign in with a saved account and check that it actually worked."""
    text = _text(page)
    if SIGNUP_WORDS.search(text) and not LOGIN_WORDS.search(text):
        _click_text(page, re.compile(r"^(sign ?in|log ?in|already have an account.*)$", re.I))
    _fill_identity(page, creds[0], creds[1], profile)
    if not _click_text(page, SUBMIT_WORDS):
        return "failed"
    result = _verify_email(page, host, cfg, since, wait_for_code)
    if result == "ok" and still_on_login(page):
        err = re.search(r"[^.\n]*(invalid|incorrect|wrong|not (found|exist|activated|verified)|activate)[^.\n]*", _text(page), re.I)
        log.info("Sign-in at %s didn't take%s", host, f": {err.group(0).strip()[:120]}" if err else "")
        return "needs_you" if err and re.search(r"activat|verif", err.group(0), re.I) else "failed"
    return result


def _verify_email(page, host: str, cfg: dict, since, wait_for_code) -> str:
    if PHONE_CHECK.search(_text(page)):
        from . import human_assist_interceptor as hia
        if hia.detect(page) != "sms" or not _human(page, "sms"):
            return "needs_you"
        page.wait_for_timeout(1500)
    if not CODE_PAGE.search(_text(page)):
        return "ok"
    found = wait_for_code(since, host, timeout=float(cfg.get("code_wait_seconds", 60)))
    if not found:
        log.info("No verification email from %s arrived in time.", host)
        return "needs_you"
    if "code" in found:
        _enter_code(page, found["code"])
    else:
        _try(lambda: page.goto(found["link"], wait_until="domcontentloaded"))
        page.wait_for_timeout(3000)
    return "ok" if not CODE_PAGE.search(_text(page)) else "needs_you"


def sign_in_or_register(page, ws, email: str, cfg: dict, profile: dict | None = None, wait_for_code=None) -> str:
    """'ok' when signed in (or registered and verified), else 'captcha', 'needs_you' or 'failed'."""
    from ..cv_tailor.single_template_tailor import load_profile
    from ..email_agent import otp
    wait_for_code = wait_for_code or otp.wait_for_code
    profile = profile or load_profile()
    host = _host(page)
    if _captcha(page) and not _human(page, "captcha"):
        return "captcha"
    with db.session(ws.db_path) as conn:
        saved = load_credentials(conn, ws, host)
    since = otp.now()
    text = _text(page)
    if saved:  # sign in with the account made earlier
        if SIGNUP_WORDS.search(text) and not LOGIN_WORDS.search(text):
            _click_text(page, re.compile(r"^(sign ?in|log ?in|already have an account.*)$", re.I))
        result = _sign_in(page, host, saved, profile, cfg, since, wait_for_code)
        log.info("Signed in to %s with the saved account: %s", host, result)
        return result
    if not cfg.get("enabled"):
        return "needs_you"
    if not page.locator("input[type=password]:visible").count() or not SIGNUP_WORDS.search(text):
        if not _click_text(page, TO_SIGNUP):
            return "needs_you"
    password = generate_password()
    with db.session(ws.db_path) as conn:  # save first, so the password is never lost
        save_credentials(conn, ws, host, email, password)
        db.audit(conn, "portal_account_created", host, email)
    _fill_identity(page, email, password, profile)
    if not _tick_terms(page, bool(cfg.get("accept_terms"))):
        return "needs_you"
    if _captcha(page) and not _human(page, "captcha"):  # you tick the CAPTCHA; the agent presses Create
        return "captcha"
    if not _click_text(page, SUBMIT_WORDS):
        return "failed"
    page.wait_for_timeout(1500)
    if _captcha(page) and not _human(page, "captcha"):
        return "captcha"
    result = _verify_email(page, host, cfg, since, wait_for_code)
    if result == "ok" and page.locator("input[type=password]:visible").count() and LOGIN_WORDS.search(_text(page)):
        # Many sites send you to their sign-in page once the account exists: sign in with it now.
        result = _sign_in(page, host, (email, password), profile, cfg, otp.now(), wait_for_code)
    if result == "ok":
        with db.session(ws.db_path) as conn:
            conn.execute("UPDATE portal_credentials SET verified = 1 WHERE host = ?", (host,))
        log.info("Created and verified an account at %s for %s", host, email)
    return result
