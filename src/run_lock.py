"""One run at a time: the daily sprint, a manual --discover/--stage-only/--send-ready, the
review page's 90-minute run and its "Send all ready" all share one database and one Chrome.
Two at once gave "database is locked" and two stagers fighting over Chrome."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime

STALE_HOURS = 3


def path(ws):
    return ws.data / "sprint.lock"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def holder(ws) -> dict | None:
    """Who holds the lock ({'pid', 'what', 'minutes'}), or None. A lock left by a crashed run is removed."""
    p = path(ws)
    try:
        raw = p.read_text(encoding="utf-8").split("\n")
        age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
    except OSError:
        return None
    try:
        pid = int(raw[0])
    except (ValueError, IndexError):
        pid = 0
    if age_h >= STALE_HOURS or (pid and not _alive(pid)):
        p.unlink(missing_ok=True)
        return None
    return {"pid": pid, "what": raw[1] if len(raw) > 1 else "run", "minutes": round(age_h * 60)}


def acquire(ws, what: str) -> bool:
    holder(ws)  # clears a stale lock
    try:
        fd = os.open(str(path(ws)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, f"{os.getpid()}\n{what}".encode())
    os.close(fd)
    return True


def release(ws) -> None:
    path(ws).unlink(missing_ok=True)


@contextmanager
def held(ws, what: str, wait_minutes: float = 0, on_wait=None):
    """with held(ws, 'stage'): ...  Waits up to wait_minutes for another run to finish; raises
    RuntimeError if it's still going."""
    deadline = time.time() + wait_minutes * 60
    while not acquire(ws, what):
        h = holder(ws) or {}
        if time.time() >= deadline:
            raise RuntimeError(f"another run ({h.get('what', 'run')}, started {h.get('minutes', 0)} min ago) is still going")
        if on_wait:
            on_wait(h)
        time.sleep(15)
    try:
        yield
    finally:
        release(ws)
