"""Encrypts portal passwords for your Windows user only (Windows DPAPI, built into Windows).

On Windows the stored value can only be decrypted by your own Windows account on this
laptop. Elsewhere (tests, macOS/Linux) it falls back to a key in the workspace folder.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import secrets
import string
from pathlib import Path


def generate_password(length: int = 20) -> str:
    """Strong random password with every character class most sites demand."""
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%^&*-_+=?"]
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice("".join(pools)) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


if os.name == "nt":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _call(fn, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in, blob_out = _Blob(len(data), buf), _Blob()
        if not fn(ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out)):
            raise OSError("Windows could not encrypt/decrypt the secret")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)

    def protect(text: str, ws=None) -> str:
        return "dpapi:" + base64.b64encode(_call(ctypes.windll.crypt32.CryptProtectData, text.encode())).decode()

    def unprotect(value: str, ws=None) -> str:
        if value.startswith("dpapi:"):
            return _call(ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(value[6:])).decode()
        return _fallback_unprotect(value, ws)
else:
    def protect(text: str, ws=None) -> str:
        return _fallback_protect(text, ws)

    def unprotect(value: str, ws=None) -> str:
        return _fallback_unprotect(value, ws)


def _key(ws) -> bytes:
    path = Path(ws.data if ws else ".") / ".secret_key"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
    return path.read_bytes()


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def _fallback_protect(text: str, ws) -> str:
    nonce, data = secrets.token_bytes(16), text.encode()
    enc = bytes(a ^ b for a, b in zip(data, _stream(_key(ws), nonce, len(data))))
    return "local:" + base64.b64encode(nonce + enc).decode()


def _fallback_unprotect(value: str, ws) -> str:
    raw = base64.b64decode(value.split(":", 1)[1])
    nonce, enc = raw[:16], raw[16:]
    return bytes(a ^ b for a, b in zip(enc, _stream(_key(ws), nonce, len(enc)))).decode()
