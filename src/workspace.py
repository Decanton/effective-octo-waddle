"""Creates and manages the JobSprintAgent working folder on the Desktop.

All runtime data lives there: the SQLite database, logs, tailored CVs,
screenshots and the saved browser profile. Works on Windows (including a
OneDrive-redirected Desktop), macOS and Linux.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FOLDER_NAME = os.environ.get("JOBSPRINT_FOLDER_NAME", "JobSprintAgent")
SUBFOLDERS = ("data", "logs", "tailored_cvs", "screenshots", "browser_profile", "exports")


def find_desktop() -> Path:
    """Return the user's real Desktop folder."""
    override = os.environ.get("JOBSPRINT_DESKTOP_DIR")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    candidates = []
    if os.name == "nt":
        try:  # Ask Windows where the Desktop really is (handles OneDrive redirection).
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:
                candidates.append(Path(buf.value))
        except Exception:
            pass
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.environ.get(var):
                candidates.append(Path(os.environ[var]) / "Desktop")
    candidates.append(home / "Desktop")
    for c in candidates:
        if c.is_dir():
            return c
    return home / "Desktop"


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def data(self) -> Path: return self.root / "data"
    @property
    def logs(self) -> Path: return self.root / "logs"
    @property
    def tailored_cvs(self) -> Path: return self.root / "tailored_cvs"
    @property
    def screenshots(self) -> Path: return self.root / "screenshots"
    @property
    def browser_profile(self) -> Path: return self.root / "browser_profile"
    @property
    def exports(self) -> Path: return self.root / "exports"
    @property
    def db_path(self) -> Path: return self.data / "jobsprint.sqlite3"


def ensure_workspace(root: Path | None = None) -> Workspace:
    """Create the workspace and all subfolders if they are missing. Safe to run repeatedly."""
    root = Path(root) if root else find_desktop() / FOLDER_NAME
    root.mkdir(parents=True, exist_ok=True)
    for sub in SUBFOLDERS:
        (root / sub).mkdir(exist_ok=True)
    readme = root / "README.txt"
    if not readme.exists():
        readme.write_text(
            "JobSprintAgent working folder\n\n"
            "Created by the job-sprint-agent project. It holds your local data:\n"
            "  data\\            the SQLite database\n"
            "  logs\\            one log file per sprint\n"
            "  tailored_cvs\\    one tailored CV (.docx) per job\n"
            "  screenshots\\     a screenshot of each filled form before Submit\n"
            "  browser_profile\\ saved browser logins (keep this private)\n"
            "  exports\\         anything you export from the review page\n",
            encoding="utf-8")
    return Workspace(root)
