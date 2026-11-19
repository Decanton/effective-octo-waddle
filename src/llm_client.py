"""Free LLM router: Gemini free tier, then Groq free tier, then a local Ollama model.

Plain HTTP calls, no paid services. Keys come from .env:
  GEMINI_API_KEY, GEMINI_MODEL (default gemini-2.5-flash)
  GROQ_API_KEY,   GROQ_MODEL   (default llama-3.3-70b-versatile)
  OLLAMA_MODEL    (e.g. llama3.1:8b), OLLAMA_HOST (default http://127.0.0.1:11434)
If none is available, callers fall back to deterministic behaviour.
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("jobsprint")


class LLMUnavailable(RuntimeError):
    pass


def _gemini(prompt: str, system: str, timeout: float) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMUnavailable("no GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800}}
    r = requests.post(url, params={"key": key}, json=body, timeout=timeout)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def _groq(prompt: str, system: str, timeout: float) -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMUnavailable("no GROQ_API_KEY")
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "temperature": 0.2, "max_tokens": 800,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
                      timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _ollama(prompt: str, system: str, timeout: float) -> str:
    model = os.environ.get("OLLAMA_MODEL")
    if not model:
        raise LLMUnavailable("no OLLAMA_MODEL")
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    r = requests.post(f"{host}/api/chat", json={"model": model, "stream": False, "options": {"temperature": 0.2},
                                                "messages": [{"role": "system", "content": system},
                                                             {"role": "user", "content": prompt}]}, timeout=timeout)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


PROVIDERS = (("gemini", _gemini), ("groq", _groq), ("ollama", _ollama))


def complete(prompt: str, system: str = "You are a careful assistant.", timeout: float = 60, retries: int = 2) -> str:
    """Try each free provider in order, with a short backoff on rate limits. Raises LLMUnavailable."""
    errors = []
    for name, fn in PROVIDERS:
        for attempt in range(retries + 1):
            try:
                return fn(prompt, system, timeout)
            except LLMUnavailable as exc:
                errors.append(f"{name}: {exc}")
                break
            except requests.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if code == 429 and attempt < retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                errors.append(f"{name}: HTTP {code}")
                break
            except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                break
    raise LLMUnavailable("; ".join(errors))


def available() -> bool:
    return any(os.environ.get(k) for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OLLAMA_MODEL"))
