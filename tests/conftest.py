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
