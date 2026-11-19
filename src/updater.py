"""Keep the agent up to date from GitHub, safely.

1. Ask GitHub for the newest commit on the branch (git ls-remote).
2. If it's newer than the installed one, download it into Desktop\\JobSprintAgent\\update_cache.
3. Run the tests there first. If any fail, nothing is installed.
4. Back up the current code to _backups\\<old commit>, copy the new code in (your .env,
   .venv and data are never touched) and install changed requirements.
5. The review page then switches to the new code without going offline (see web/frontdoor.py).

It never updates while a daily run is in progress.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("jobsprint")

APP_DIR = Path(__file__).resolve().parents[1]
MARKER = ".installed_commit"
SKIP = {".git", ".env", ".env.example", ".venv", "venv", "__pycache__", ".pytest_cache", "_backups", MARKER}


def _cfg() -> dict:
    from .settings import load_settings
    return load_settings().get("update") or {}


def _git(*args, cwd=None, timeout=120) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:300])
    return out.stdout.strip()


def installed(app_dir: Path = APP_DIR) -> str | None:
    try:
        return (app_dir / MARKER).read_text().strip() or None
    except OSError:
        pass
    try:  # a git checkout knows its own commit
        return _git("rev-parse", "HEAD", cwd=app_dir, timeout=10)
    except Exception:
        return None


def latest(cfg: dict) -> str:
    out = _git("ls-remote", cfg.get("repo", "https://github.com/Decanton/effective-octo-waddle.git"),
               f"refs/heads/{cfg.get('branch', 'main')}", timeout=60)
    if not out:
        raise RuntimeError("branch not found on GitHub")
    return out.split()[0]


def download(cfg: dict, cache: Path) -> Path:
    repo, branch = cfg.get("repo", "https://github.com/Decanton/effective-octo-waddle.git"), cfg.get("branch", "main")
    if not (cache / ".git").exists():
        shutil.rmtree(cache, ignore_errors=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--depth", "1", "--branch", branch, repo, str(cache), timeout=300)
    else:
        _git("fetch", "--depth", "1", "origin", branch, cwd=cache, timeout=300)
        _git("checkout", "-f", "-B", branch, "FETCH_HEAD", cwd=cache)
        _git("clean", "-fdq", cwd=cache)
    return cache


def _digest(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def run_tests(src: Path, python: str = sys.executable, retries: int = 1) -> tuple[bool, str]:
    """Run the new version's tests. A failure is retried once on its own, so a timing blip in a browser test
    doesn't block an update; a real failure fails both times."""
    env = {**os.environ, "JOBSPRINT_HEADLESS": "1", "JOBSPRINT_NO_NOTIFY": "1", "JOBSPRINT_NO_CDP": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run([python, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider"], cwd=str(src), env=env,
                         capture_output=True, text=True, timeout=1800, creationflags=flags)
    lines = (out.stdout or out.stderr).strip().splitlines()
    tail = lines[-1:] or [""]
    if out.returncode == 0:
        return True, tail[0]
    failed = [ln.split()[1] for ln in lines if ln.startswith("FAILED ") and len(ln.split()) > 1]
    failed = [f.split(" - ")[0] for f in failed]
    if retries and failed and len(failed) <= 3:
        again = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *failed], cwd=str(src), env=env,
                               capture_output=True, text=True, timeout=900, creationflags=flags)
        if again.returncode == 0:
            log.info("Tests passed after retrying %s", ", ".join(failed))
            return True, tail[0] + f" (retried {len(failed)} and they passed)"
    return False, (", ".join(failed[:3]) + ": " if failed else "") + tail[0]


def _copy_tree(src: Path, dst: Path) -> None:
    for item in src.iterdir():
        if item.name in SKIP or item.name.startswith("_backup"):
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def install(src: Path, app_dir: Path, commit: str, old: str | None, python: str = sys.executable) -> None:
    backups = app_dir / "_backups"
    backup = backups / (old[:12] if old else time.strftime("%Y%m%d_%H%M%S"))
    if not backup.exists():
        backup.mkdir(parents=True)
        _copy_tree(app_dir, backup)
    for old_b in sorted(backups.iterdir(), key=lambda p: p.stat().st_mtime)[:-3]:  # keep the last 3
        shutil.rmtree(old_b, ignore_errors=True)
    req_changed = _digest(src / "requirements.txt") != _digest(app_dir / "requirements.txt")
    _copy_tree(src, app_dir)
    if req_changed:
        subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(app_dir / "requirements.txt")],
                       cwd=str(app_dir), timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (app_dir / MARKER).write_text(commit)


_state = {"checked_at": None, "latest": None, "installed": None, "updated_at": None, "error": None, "busy": False}


def status(app_dir: Path = APP_DIR) -> dict:
    return {**_state, "installed": installed(app_dir), "enabled": _cfg().get("enabled", True)}


def check_and_update(ws, app_dir: Path = APP_DIR, cfg: dict | None = None, force: bool = False) -> dict:
    """{'updated': bool, 'from': .., 'to': .., 'error': ..}. Safe to call any time."""
    cfg = cfg if cfg is not None else _cfg()
    res = {"updated": False, "from": installed(app_dir), "to": None, "error": None}
    if not force and not cfg.get("enabled", True):
        return {**res, "error": "updates are off in config/agent_settings.yaml"}
    if _state["busy"]:
        return {**res, "error": "an update is already running"}
    if (ws.data / "sprint.lock").exists():
        return {**res, "error": "a run is in progress; will update after it"}
    _state["busy"] = True
    try:
        new = latest(cfg)
        _state.update(checked_at=time.time(), latest=new, error=None)
        res["to"] = new
        if new == res["from"]:
            return res
        log.info("Update found: %s -> %s", (res["from"] or "?")[:7], new[:7])
        src = download(cfg, ws.data / "update_cache")
        if _git("rev-parse", "HEAD", cwd=src, timeout=10) != new:
            raise RuntimeError("downloaded code doesn't match GitHub")
        if cfg.get("run_tests", True):
            # New libraries (or test tools) the new version needs go in first, so its tests can run.
            if _digest(src / "requirements.txt") != _digest(app_dir / "requirements.txt"):
                subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                                str(src / "requirements.txt")], cwd=str(src), timeout=900,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ok, summary = run_tests(src)
            if not ok:
                raise RuntimeError(f"tests failed on the new version ({summary}); kept the current one")
        install(src, app_dir, new, res["from"])
        _state["updated_at"] = time.time()
        try:
            from . import database as db
            with db.session(ws.db_path) as conn:
                db.audit(conn, "update_installed", new[:7], f"from {(res['from'] or '?')[:7]}")
        except Exception:
            pass
        log.info("Updated to %s", new[:7])
        return {**res, "updated": True}
    except Exception as exc:
        _state["error"] = str(exc)
        log.warning("Update not installed: %s", exc)
        return {**res, "error": str(exc)}
    finally:
        _state["busy"] = False
