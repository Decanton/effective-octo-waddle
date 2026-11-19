"""Copies your everyday Chrome profile (e.g. "Dickens Owuor") into the agent's folder.

Chrome refuses to let automation tools drive your real profile, but it does allow a
copy in another folder. The copy keeps your sign-ins (Google included), so the agent
opens job sites already signed in and can use "Continue with Google".

What is copied: cookies, site storage and settings for that one profile.
What is NOT copied: saved passwords, saved cards and autofill, history, extensions, caches.
Chrome must be closed while copying (its files are locked while it runs).
Run again any time to refresh the copy:  START_SPRINT.bat --use-chrome-profile "Dickens Owuor"
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("jobsprint")

SKIP = {"Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache",
        "ShaderCache", "Service Worker", "Extensions", "Extension State", "Extension Rules", "Extension Scripts",
        "Local Extension Settings", "Sync Extension Settings", "Managed Extension Settings", "blob_storage",
        "History", "History-journal", "Visited Links", "Top Sites", "Top Sites-journal", "Favicons", "Favicons-journal",
        "Login Data", "Login Data-journal", "Login Data For Account", "Login Data For Account-journal",
        "Web Data", "Web Data-journal", "Shortcuts", "Shortcuts-journal", "Sessions", "Current Session",
        "Current Tabs", "Last Session", "Last Tabs", "optimization_guide_model_store", "Download Service"}


def user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def list_profiles(src: Path | None = None) -> dict[str, str]:
    """{display name: folder name}, e.g. {"Dickens Owuor": "Profile 2"}."""
    src = src or user_data_dir()
    state = json.loads((src / "Local State").read_text(encoding="utf-8"))
    cache = (state.get("profile") or {}).get("info_cache") or {}
    return {(v.get("name") or k): k for k, v in cache.items()}


def find_profile(name: str, src: Path | None = None) -> str:
    profiles = list_profiles(src)
    for display, folder in profiles.items():
        if display.lower() == name.lower() or folder.lower() == name.lower():
            return folder
    for display, folder in profiles.items():
        if name.lower() in display.lower():
            return folder
    raise LookupError(f"No Chrome profile called '{name}'. Found: {', '.join(profiles) or 'none'}")


def chrome_running() -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"], capture_output=True, text=True).stdout
    return "chrome.exe" in out.lower()


def clone(ws, name: str, src: Path | None = None, check_running: bool = True) -> Path:
    """Copy profile `name` into ws.root/chrome_profile and remember it for the agent."""
    src = src or user_data_dir()
    folder = find_profile(name, src)
    if check_running and chrome_running():
        raise RuntimeError("Chrome is open. Close every Chrome window (check the tray too), then run this again.")
    dest = ws.root / "chrome_profile"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copy2(src / "Local State", dest / "Local State")
    shutil.copytree(src / folder, dest / folder, ignore=lambda d, names: [n for n in names if n in SKIP],
                    ignore_dangling_symlinks=True, dirs_exist_ok=True)
    (dest / "jobsprint_profile.json").write_text(json.dumps({"name": name, "folder": folder}), encoding="utf-8")
    log.info("Copied Chrome profile '%s' (%s) to %s. Saved passwords, cards and history were not copied.", name, folder, dest)
    return dest


def cloned(ws) -> tuple[Path, str] | None:
    """(user data dir, profile folder) of the copied profile, if there is one."""
    meta = ws.root / "chrome_profile" / "jobsprint_profile.json"
    if not meta.exists():
        return None
    folder = json.loads(meta.read_text(encoding="utf-8"))["folder"]
    return meta.parent, folder
