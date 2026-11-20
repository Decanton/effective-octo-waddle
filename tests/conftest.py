"""Tests never reach the internet, so they give the same result on any machine."""
from urllib.parse import urlparse

import pytest
import requests


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch):
    real = requests.sessions.Session.request

    def guarded(self, method, url, *a, **k):
        if urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise requests.ConnectionError(f"tests don't use the internet ({url})")
        return real(self, method, url, *a, **k)
    monkeypatch.setattr(requests.sessions.Session, "request", guarded)


# Real keys and mail settings from .env (loaded when the updater runs from run_sprint.py) must not change
# what the tests do: with EMAIL_ADDRESS set, the stager would try "Continue with Google", for example.
SECRETS = ("GEMINI_API_KEY", "GROQ_API_KEY", "OLLAMA_MODEL", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD",
           "IMAP_HOST", "IMAP_PORT", "SMTP_HOST", "SMTP_PORT")


@pytest.fixture(autouse=True)
def _no_real_accounts(monkeypatch):
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _review_mode_off(monkeypatch):
    """config's review_from is a real date; tests must behave the same before and after it."""
    monkeypatch.setenv("JOBSPRINT_TEST_CLOCK_FREE", "1")
