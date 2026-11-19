"""Job Sprint Agent: main entry point.

  python run_sprint.py              daily sprint: inbox + job search, fill forms, send the complete
                                    ones (see config/agent_settings.yaml), then open the review page
  python run_sprint.py --setup      create the Desktop folder and database only
  python run_sprint.py --check      check your profile and settings
  python run_sprint.py --sample-cvs build one sample CV per track to review the layout
  python run_sprint.py --cv-for FILE  build a tailored CV for a job description saved in FILE
  python run_sprint.py --discover   find jobs from the last 24 hours and rank them
  python run_sprint.py --mark-applied NAME  mark an AI platform (e.g. AfterQuery) as applied
  python run_sprint.py --login-setup  open the saved browser so you can sign in to sites once
  python run_sprint.py --stage-only   fill forms for the top qualified jobs (stops before Submit)
  python run_sprint.py --review       open the review page at http://127.0.0.1:8000
  python run_sprint.py --stage-limit N  stage at most N jobs this run
  python run_sprint.py --send-ready   send the complete applications now (same rules as the daily run)
  python run_sprint.py --email-only   read the inbox: job alerts into the list, recruiter replies drafted
  python run_sprint.py --no-send      daily sprint without sending anything this time
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import database as db  # noqa: E402
from src.workspace import ensure_workspace  # noqa: E402

PLANNED: dict[str, str] = {}


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def _logger(ws) -> logging.Logger:
    log = logging.getLogger("jobsprint")
    if not log.handlers:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
        fh = logging.FileHandler(ws.logs / f"sprint_{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
        sh = logging.StreamHandler(sys.stdout)
        for h in (fh, sh):
            h.setFormatter(fmt)
            log.addHandler(h)
    return log


def setup():
    ws = ensure_workspace()
    db.init_db(ws.db_path)
    from resumes.build_master_template import TEMPLATE_PATH, build
    if not TEMPLATE_PATH.exists():
        build(TEMPLATE_PATH)
    return ws


def check(log) -> int:
    """Validate the profile and settings. Returns the number of problems."""
    from src.cv_tailor.single_template_tailor import TRACKS, load_profile
    problems = 0
    try:
        profile = load_profile()
    except (OSError, json.JSONDecodeError) as exc:
        log.error("config/master_profile.json could not be read: %s", exc)
        return 1
    for key in ("full_name", "email", "phone", "location"):
        if not profile["personal"].get(key):
            log.error("Profile is missing personal.%s", key)
            problems += 1
    for t in TRACKS:
        if t not in profile["summaries"] or t not in profile["skills"]:
            log.error("Profile is missing the summary or skills for track %s", t)
            problems += 1
    missing = [k for k, v in profile["screening_answers"].items() if v is None and not k.startswith("_")]
    if missing:
        log.warning("These screening answers are empty, so forms asking them will be flagged "
                    "NEEDS_USER_INPUT: %s", ", ".join(missing))
    if not (ROOT / ".env").exists():
        log.warning("No .env file yet. Copy .env.example to .env before steps 3 and 4.")
    log.info("Profile check finished with %d problem(s).", problems)
    return problems


def sample_cvs(ws, log) -> list[Path]:
    from src.cv_tailor.single_template_tailor import TRACKS, build_cv
    out = []
    for t in TRACKS:
        path, cv = build_cv("", ws.tailored_cvs / "_samples" / f"Dickens_Owuor_{t}.docx", track=t)
        log.info("Sample CV (%s): %s", t, path)
        out.append(path)
    return out


def cv_for(ws, log, job_file: Path) -> Path:
    from src.cv_tailor.single_template_tailor import build_cv
    text = Path(job_file).read_text(encoding="utf-8", errors="ignore")
    path, cv = build_cv(text, ws.tailored_cvs / f"{Path(job_file).stem}.docx")
    log.info("Tailored CV (%s track) written to %s", cv.track, path)
    log.info("Matched keywords: %s", ", ".join(cv.matched_keywords[:25]) or "none")
    return path


def discover(ws, log) -> int:
    from src.discovery import pipeline
    with db.session(ws.db_path) as conn:
        run_id = db.start_run(conn, "discover")
    result = pipeline.run(ws, run_id)
    with db.session(ws.db_path) as conn:
        db.finish_run(conn, run_id, jobs_found=result["found"], jobs_kept=result["kept"], emails_found=result.get("emails", 0))
    return 0


def email_only(ws, log) -> int:
    from src.discovery import pipeline
    with db.session(ws.db_path) as conn:
        run_id = db.start_run(conn, "email")
    result = pipeline.run(ws, run_id, sources=("email",))
    with db.session(ws.db_path) as conn:
        db.finish_run(conn, run_id, jobs_found=result["found"], jobs_kept=result["kept"], emails_found=result["emails"])
    return 0


def stage(ws, log, limit=None) -> dict:
    from src.automation import form_stager
    with db.session(ws.db_path) as conn:
        run_id = db.start_run(conn, "stage")
    counts = form_stager.run(ws, limit)
    with db.session(ws.db_path) as conn:
        db.finish_run(conn, run_id, jobs_staged=counts.get("READY_TO_SUBMIT", 0) + counts.get("NEEDS_USER_INPUT", 0),
                      notes=json.dumps(counts))
        ready = conn.execute("SELECT COUNT(*) FROM staged_forms WHERE state = 'READY_TO_SUBMIT'").fetchone()[0]
    target = int(json.loads((ROOT / "config" / "master_profile.json").read_text(encoding="utf-8"))
                 .get("workspace", {}).get("daily_stage_target", 20))
    if ready >= target:
        log.info("%d applications are ready to submit (target %d). Time to review!", ready, target)
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Local job application and recruiter email agent")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sample-cvs", action="store_true")
    ap.add_argument("--cv-for", type=Path, metavar="FILE")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--mark-applied", metavar="PLATFORM")
    ap.add_argument("--stage-only", action="store_true")
    ap.add_argument("--stage-limit", type=int)
    ap.add_argument("--restage", action="store_true", help="retry jobs that ended as MANUAL_APPLY, FAILED or NEEDS_LOGIN")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--no-review", action="store_true", help="don't open the review page at the end")
    ap.add_argument("--login-setup", action="store_true")
    ap.add_argument("--email-only", action="store_true")
    ap.add_argument("--send-ready", action="store_true", help="send complete applications now")
    ap.add_argument("--no-send", action="store_true", help="don't send anything this run")
    args = ap.parse_args(argv)
    _load_env()

    for flag, msg in PLANNED.items():
        if getattr(args, flag):
            print(msg)
            return 0

    ws = setup()
    log = _logger(ws)
    log.info("Workspace ready at %s", ws.root)
    if args.setup:
        return 0
    if args.check:
        return 1 if check(log) else 0
    if args.sample_cvs:
        sample_cvs(ws, log)
        return 0
    if args.cv_for:
        cv_for(ws, log, args.cv_for)
        return 0
    if args.mark_applied:
        from src.discovery.ai_platforms import mark_platform
        with db.session(ws.db_path) as conn:
            mark_platform(conn, args.mark_applied, "APPLIED")
        log.info("Marked %s as applied.", args.mark_applied)
        return 0
    if args.discover:
        return discover(ws, log)
    if args.login_setup:
        from src.automation.browser_manager import login_setup
        login_setup(ws)
        return 0
    if args.restage:
        with db.session(ws.db_path) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE status IN ('MANUAL_APPLY','FAILED','NEEDS_LOGIN')")]
            for jid in ids:
                conn.execute("DELETE FROM staged_forms WHERE job_id = ?", (jid,))
                db.set_job_status(conn, jid, "QUALIFIED", "queued again for staging")
        log.info("Queued %d jobs to be staged again.", len(ids))
        if not args.stage_only:
            return 0
    if args.stage_only:
        stage(ws, log, args.stage_limit)
        return 0
    if args.send_ready:
        from src.automation import auto_submitter
        auto_submitter.run(ws)
        return 0
    if args.email_only:
        return email_only(ws, log)
    if args.review:
        from src.web.server import serve
        serve(ws)
        return 0

    # Daily sprint: check profile, find and rank jobs, stage forms, then open the review page.
    if check(log):
        log.error("Fix the profile problems above, then run again.")
        return 1
    discover(ws, log)
    stage(ws, log, args.stage_limit)
    if not args.no_send:
        from src.automation import auto_submitter
        auto_submitter.run(ws)
    if not args.no_review:
        from src.web.server import serve
        serve(ws)
    return 0


if __name__ == "__main__":
    sys.exit(main())
