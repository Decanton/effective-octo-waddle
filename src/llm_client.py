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


_picked: dict[str, str] = {}  # provider -> model that works, found after a 404


def _rank_models(names: list[str], prefer: tuple[str, ...]) -> list[str]:
    """Prefer cheap, fast, stable models; skip previews, experiments and non-chat models."""
    bad = ("embed", "vision", "tts", "audio", "image", "whisper", "guard", "exp", "preview", "aqa", "learnlm", "imagen", "veo")
    ok = [n for n in names if not any(b in n.lower() for b in bad)]
    def score(n):
        n = n.lower()
        return (next((i for i, p in enumerate(prefer) if p in n), len(prefer)), "lite" in n, -len(n), n)
    return sorted(ok, key=score)


def gemini_models(key: str, timeout: float = 20) -> list[str]:
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key, "pageSize": 200}, timeout=timeout)
    r.raise_for_status()
    names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    return _rank_models(names, ("flash", "pro", "gemini"))


def groq_models(key: str, timeout: float = 20) -> list[str]:
    r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    r.raise_for_status()
    names = [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
    return _rank_models(names, ("llama-3.3-70b", "llama", "gpt-oss", "qwen", "mixtral", "gemma"))


def _with_model_fallback(provider: str, default: str, env: str, call, list_models, key: str):
    """Use the configured model; if the provider says it doesn't exist (404), pick one it lists."""
    model = _picked.get(provider) or os.environ.get(env) or default
    try:
        return call(model)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in (400, 404):
            raise
        tried = model
    for m in list_models(key)[:4]:
        if m == tried:
            continue
        try:
            out = call(m)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (400, 404):
                continue
            raise
        _picked[provider] = m
        log.info("%s model '%s' isn't available; using '%s' instead (set %s in .env to choose).", provider, tried, m, env)
        return out
    raise LLMUnavailable(f"model '{tried}' not found and no listed model worked")


def _gemini(prompt: str, system: str, timeout: float) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMUnavailable("no GEMINI_API_KEY")
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800}}

    def call(model):
        model = model.split("/", 1)[-1]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = requests.post(url, params={"key": key.strip()}, json=body, timeout=timeout)
        r.raise_for_status()
        return r
    r = _with_model_fallback("gemini", "gemini-2.5-flash", "GEMINI_MODEL", call, gemini_models, key.strip())
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def _groq(prompt: str, system: str, timeout: float) -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMUnavailable("no GROQ_API_KEY")
    key = key.strip()

    def call(model):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model, "temperature": 0.2, "max_tokens": 800,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
                          timeout=timeout)
        r.raise_for_status()
        return r
    r = _with_model_fallback("groq", "llama-3.3-70b-versatile", "GROQ_MODEL", call, groq_models, key)
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
