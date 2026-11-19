"""Local review page at http://127.0.0.1:8000 (only reachable from this laptop)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .. import database as db

HERE = Path(__file__).resolve().parent
_submit_lock = threading.Lock()  # one browser at a time


def create_app(ws) -> FastAPI:
    import time
    from .. import updater
    _started, _commit = time.time(), updater.installed()  # the code this process is running
    app = FastAPI(title="Job Sprint review", docs_url=None, redoc_url=None)

    def _inside_ws(p: str) -> Path:
        path = Path(p).resolve()
        if ws.root.resolve() not in path.parents:
            raise HTTPException(403, "outside the workspace")
        if not path.exists():
            raise HTTPException(404, "file missing")
        return path

    @app.get("/api/version")
    def version():
        return {"commit": _commit, "started_at": _started}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (HERE / "index.html").read_text(encoding="utf-8")

    @app.get("/api/staged")
    def staged():
        with db.session(ws.db_path) as c:
            rows = c.execute("""
                SELECT s.id, s.state, s.form_url, s.answers_json, s.missing_json, s.error, s.created_at, s.submitted_at, s.receipt_json,
                       j.id AS job_id, j.title, j.company, j.location, j.age_hours, j.applicant_count, j.track,
                       j.priority_score, j.fit_score, j.url, j.source, t.docx_path, t.matched_keywords,
                       (SELECT path FROM screenshots WHERE staged_form_id = s.id ORDER BY id DESC LIMIT 1) AS shot
                FROM staged_forms s JOIN jobs j ON j.id = s.job_id LEFT JOIN tailored_cvs t ON t.id = s.cv_id
                WHERE s.state IN ('READY_TO_SUBMIT','NEEDS_USER_INPUT','MANUAL_APPLY','NEEDS_LOGIN','NEEDS_MANUAL_OTP','SENT_UNCONFIRMED')
                   OR (s.state = 'SUBMITTED' AND s.submitted_at >= date('now', '-14 days'))
                ORDER BY CASE s.state WHEN 'READY_TO_SUBMIT' THEN 0 WHEN 'NEEDS_USER_INPUT' THEN 1 WHEN 'SENT_UNCONFIRMED' THEN 2
                         WHEN 'NEEDS_MANUAL_OTP' THEN 3 WHEN 'NEEDS_LOGIN' THEN 3 WHEN 'MANUAL_APPLY' THEN 4 ELSE 5 END,
                         s.submitted_at DESC, j.priority_score DESC""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["answers"] = json.loads(d.pop("answers_json") or "[]")
            d["missing"] = json.loads(d.pop("missing_json") or "[]")
            d["has_shot"] = bool(d.pop("shot"))
            d["has_cv"] = bool(d.pop("docx_path"))
            d["receipt"] = json.loads(d.pop("receipt_json") or "null")
            out.append(d)
        return out

    @app.get("/api/summary")
    def summary():
        with db.session(ws.db_path) as c:
            forms = {r[0]: r[1] for r in c.execute("SELECT state, COUNT(*) FROM staged_forms GROUP BY state")}
            from ..automation.auto_submitter import sent_today
            from ..settings import load_settings
            st = load_settings()
            last = c.execute("SELECT started_at, finished_at, mode, jobs_found, jobs_kept FROM sprint_runs "
                             "ORDER BY id DESC LIMIT 1").fetchone()
            return {"forms": forms, "jobs": db.count_by_status(c),
                    "sent_today": {"forms": sent_today(c), "email": sent_today(c, by_email=True)},
                    "settings": {"auto_forms": st["auto_submit"]["enabled"], "forms_per_day": st["auto_submit"]["max_per_day"],
                                 "auto_email": st["email_applications"]["enabled"], "email_per_day": st["email_applications"]["max_per_day"],
                                 "google": st["google_sign_in"]["enabled"]},
                    "last_run": dict(last) if last else None,
                    "emails_waiting": c.execute("SELECT COUNT(*) FROM recruiter_emails WHERE state = 'DRAFTED'").fetchone()[0],
                    "task": _task_status()}

    @app.get("/files/screenshot/{sid}")
    def screenshot(sid: int):
        with db.session(ws.db_path) as c:
            r = c.execute("SELECT path FROM screenshots WHERE staged_form_id = ? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if not r:
            raise HTTPException(404)
        return FileResponse(_inside_ws(r[0]))

    @app.get("/files/cv/{sid}")
    def cv(sid: int):
        with db.session(ws.db_path) as c:
            r = c.execute("SELECT t.docx_path FROM staged_forms s JOIN tailored_cvs t ON t.id = s.cv_id WHERE s.id = ?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404)
        p = _inside_ws(r[0])
        return FileResponse(p, filename=p.name)

    @app.post("/api/submit/{sid}")
    def submit(sid: int, payload: dict = Body(default={})):
        from ..automation.form_submitter import submit as do_submit
        if not _submit_lock.acquire(blocking=False):
            raise HTTPException(409, "another submission is running")
        try:
            return do_submit(ws, sid, payload.get("overrides"), payload.get("consents"), attended=True)
        finally:
            _submit_lock.release()

    _bulk = {"running": False, "result": None}
    _task = {"proc": None, "what": None}

    def _task_status():
        p = _task["proc"]
        return {"running": bool(p and p.poll() is None), "what": _task["what"]}

    @app.post("/api/run/{what}")
    def run_task(what: str):
        """Start the daily run or the sign-in window in its own console window."""
        import subprocess
        import sys
        args = {"sprint": ["--no-review"], "login": ["--login-setup"]}.get(what)
        if args is None:
            raise HTTPException(404)
        if _task_status()["running"]:
            raise HTTPException(409, f"'{_task['what']}' is still running")
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        root = HERE.parents[1]
        _task["proc"] = subprocess.Popen([sys.executable, str(root / "run_sprint.py"), *args], cwd=str(root), creationflags=flags)
        _task["what"] = what
        return {"ok": True}

    @app.post("/api/submit_all")
    def submit_all():
        """'Send all ready': you approve every complete form at once."""
        from ..automation import auto_submitter
        from ..settings import load_settings
        if _bulk["running"] or not _submit_lock.acquire(blocking=False):
            raise HTTPException(409, "a submission is already running")
        settings = load_settings()
        settings["auto_submit"] = {**settings["auto_submit"], "enabled": True, "only_hosts": [""],
                                   "max_per_day": 10_000}
        settings["email_applications"] = {**settings["email_applications"], "enabled": True, "max_per_day": 10_000}
        _bulk["running"] = True

        def work():
            try:
                _bulk["result"] = auto_submitter.run(ws, settings)
            finally:
                _bulk["running"] = False
                _submit_lock.release()
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    @app.get("/api/submit_all")
    def submit_all_status():
        return _bulk

    def _set(sid: int, form_state: str, job_status: str, reason: str):
        with db.session(ws.db_path) as c:
            r = c.execute("SELECT job_id FROM staged_forms WHERE id = ?", (sid,)).fetchone()
            if not r:
                raise HTTPException(404)
            c.execute("UPDATE staged_forms SET state = ?, submitted_at = CASE WHEN ? = 'SUBMITTED' THEN ? ELSE submitted_at END WHERE id = ?",
                      (form_state, form_state, db.now_iso(), sid))
            db.set_job_status(c, r[0], job_status, reason)
        return {"ok": True}

    @app.post("/api/skip/{sid}")
    def skip(sid: int):
        return _set(sid, "SKIPPED", "SKIPPED", "skipped on the review page")

    @app.post("/api/applied_myself/{sid}")
    def applied_myself(sid: int):
        return _set(sid, "SUBMITTED", "SUBMITTED", "you applied yourself")

    @app.get("/api/emails")
    def emails():
        with db.session(ws.db_path) as c:
            return [dict(r) for r in c.execute(
                "SELECT id, from_addr, from_name, subject, received_at, body_text, category, draft_reply, state, sent_at "
                "FROM recruiter_emails WHERE state != 'DISMISSED' ORDER BY state = 'SENT', received_at DESC LIMIT 200")]

    @app.post("/api/emails/{eid}/send")
    def send_email(eid: int, payload: dict = Body(...)):
        """Sends your reply. Only this button sends email; nothing is sent automatically."""
        from ..email_agent.recruiter_replies import send
        with db.session(ws.db_path) as c:
            return send(c, eid, payload.get("body") or "")

    @app.post("/api/emails/{eid}/dismiss")
    def dismiss_email(eid: int):
        with db.session(ws.db_path) as c:
            c.execute("UPDATE recruiter_emails SET state = 'DISMISSED' WHERE id = ?", (eid,))
        return {"ok": True}

    @app.get("/api/platforms")
    def platforms():
        with db.session(ws.db_path) as c:
            return [dict(r) for r in c.execute("SELECT * FROM platforms ORDER BY status, name")]

    @app.post("/api/platforms/{name}")
    def set_platform(name: str, payload: dict = Body(...)):
        from ..discovery.ai_platforms import mark_platform
        with db.session(ws.db_path) as c:
            mark_platform(c, name, payload.get("status", "APPLIED"))
        return {"ok": True}

    return app


def serve(ws, open_browser: bool = True, port: int = 8000) -> None:
    import webbrowser

    import uvicorn
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\nReview page: {url}  (press Ctrl+C here to stop it)\n")
    uvicorn.run(create_app(ws), host="127.0.0.1", port=port, log_level="warning")
