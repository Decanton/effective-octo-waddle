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


def send_all(ws, log) -> None:
    """Send complete applications (forms and email) and complete recruiter replies, within the daily caps."""
    from src.automation import auto_submitter
    from src.email_agent.recruiter_replies import auto_send
    from src.settings import load_settings
    settings = load_settings()
    if auto_submitter.review_mode(settings):  # Kelly checks everything first: no applications, replies or follow-ups go out
        log.info("Review mode: nothing is sent automatically. Ready applications and reply drafts wait on the review page.")
        return
    auto_submitter.run(ws, settings)
    try:
        with db.session(ws.db_path) as conn:
            auto_send(conn, settings["recruiter_replies"])
    except Exception as exc:  # email trouble must not stop the run
        log.warning("Recruiter replies were not sent: %s", exc)
    try:
        from src.cv_tailor.single_template_tailor import load_profile
        from src.email_agent import followups
        with db.session(ws.db_path) as conn:
            followups.run(conn, settings["follow_ups"], load_profile())
    except Exception as exc:
        log.warning("Follow-ups were not sent: %s", exc)


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
    ap.add_argument("--restage", action="store_true", help="retry jobs that ended as MANUAL_APPLY, FAILED, NEEDS_LOGIN or NEEDS_MANUAL_OTP")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--review-backend", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=8000, help=argparse.SUPPRESS)
    ap.add_argument("--check-sources", action="store_true", help="try every job source once and show what each returns")
    ap.add_argument("--update", action="store_true", help="install the newest version from GitHub now (tests run first)")
    ap.add_argument("--no-update", action="store_true", help="don't check for a new version this run")
    ap.add_argument("--no-review", action="store_true", help="don't open the review page at the end")
    ap.add_argument("--login-setup", action="store_true")
    ap.add_argument("--email-only", action="store_true")
    ap.add_argument("--send-ready", action="store_true", help="send complete applications now")
    ap.add_argument("--no-send", action="store_true", help="don't send anything this run")
    ap.add_argument("--use-chrome-profile", metavar="NAME", help='copy your Chrome profile (e.g. "Dickens Owuor") for the agent')
    ap.add_argument("--list-chrome-profiles", action="store_true")
    ap.add_argument("--open-chrome", action="store_true", help="open the agent's Chrome (your copied profile) as a normal window")
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
    if args.open_chrome:
        from src.automation.browser_manager import open_chrome
        open_chrome(ws)
        return 0
    if args.list_chrome_profiles:
        from src.automation.chrome_profile import list_profiles
        for display, folder in list_profiles().items():
            print(f"{display}  ({folder})")
        return 0
    if args.use_chrome_profile:
        from src.automation.chrome_profile import clone
        try:
            clone(ws, args.use_chrome_profile)
        except (LookupError, RuntimeError, OSError) as exc:
            log.error("%s", exc)
            return 1
        log.info("The agent now opens job sites with your '%s' sign-ins.", args.use_chrome_profile)
        return 0
    if args.login_setup:
        from src.automation.browser_manager import login_setup
        login_setup(ws)
        return 0
    if args.restage:
        with db.session(ws.db_path) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE status IN ('MANUAL_APPLY','FAILED','NEEDS_LOGIN','NEEDS_MANUAL_OTP')")]
            for jid in ids:
                conn.execute("DELETE FROM staged_forms WHERE job_id = ?", (jid,))
                db.set_job_status(conn, jid, "QUALIFIED", "queued again for staging")
        log.info("Queued %d jobs to be staged again.", len(ids))
        if not args.stage_only:
            return 0
    if args.stage_only:  # fills forms and sends nothing, LinkedIn Easy Apply included
        import os as _os
        _os.environ["JOBSPRINT_NO_SUBMIT"] = "1"
        stage(ws, log, args.stage_limit)
        return 0
    if args.send_ready:
        send_all(ws, log)
        return 0
    if args.email_only:
        return email_only(ws, log)
    if args.review_backend:
        from src.web.server import serve
        serve(ws, open_browser=False, port=args.port)
        return 0
    if args.review:
        from src.web.frontdoor import serve
        serve(ws)
        return 0
    if args.check_sources:
        return check_sources(log)
    if args.update:
        from src import updater
        res = updater.check_and_update(ws, force=True)
        print("Updated to " + res["to"][:7] if res["updated"] else (res["error"] or "Already up to date."))
        return 0 if not res["error"] else 1
    if not args.no_update and _self_update(ws, log):
        import subprocess
        return subprocess.call([sys.executable, str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:]), "--no-update"])

    # Daily sprint: check profile, find and rank jobs, stage forms, then open the review page.
    lock = ws.data / "sprint.lock"
    try:
        import os as _os
        fd = _os.open(str(lock), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
        _os.write(fd, str(_os.getpid()).encode())
        _os.close(fd)
    except FileExistsError:
        age_h = (datetime.now().timestamp() - lock.stat().st_mtime) / 3600
        if age_h < 3:
            log.error("Another run started %.0f minutes ago and is still going. Try again when it finishes.", age_h * 60)
            return 1
        log.warning("Removing a stale run lock from %.1f hours ago.", age_h)
        lock.unlink(missing_ok=True)
        return main(argv)
    try:
        rc = _daily(ws, log, args)
    finally:
        lock.unlink(missing_ok=True)
    if rc == 0 and not args.no_review:
        from src.web.frontdoor import serve
        serve(ws)
    return rc


def check_sources(log) -> int:
    """Fetch every job source once and print how many postings each gave (and how many are from the last 24h)."""
    from src.discovery import more_boards, remote_boards
    from src.discovery.common import load_yaml
    cfg = load_yaml("search_queries.yaml")
    report = {}
    more_boards.fetch(cfg, report)
    for name in cfg.get("remote_boards") or []:
        try:
            found = remote_boards.fetch([name])
            report[name] = (len(found), sum(1 for j in found if (j["age_hours"] or 99) <= 48), None if found else "nothing returned")
        except Exception as exc:
            report[name] = (0, 0, str(exc)[:120])
    print(f"\n{'source':<30} {'postings':>8} {'last 48h':>8}  problem")
    for name, (n, fresh, err) in report.items():
        print(f"{name:<30} {n:>8} {fresh:>8}  {err or ''}")
    return 0


def _self_update(ws, log) -> bool:
    """Before a run: install a newer version if there is one (only when its tests pass)."""
    import os as _os
    if _os.environ.get("JOBSPRINT_HEADLESS") == "1":
        return False
    from src import updater
    from src.settings import load_settings
    if not (load_settings().get("update") or {}).get("on_run", True):
        return False
    res = updater.check_and_update(ws)
    if res["updated"]:
        log.info("Installed the new version %s; restarting this run on it.", res["to"][:7])
    return res["updated"]


def _daily(ws, log, args) -> int:
    if check(log):
        log.error("Fix the profile problems above, then run again.")
        return 1
    discover(ws, log)
    with db.session(ws.db_path) as conn:  # a CAPTCHA or SMS code nobody answered last time: try again today
        for (jid,) in conn.execute("SELECT id FROM jobs WHERE status = 'NEEDS_MANUAL_OTP'").fetchall():
            conn.execute("DELETE FROM staged_forms WHERE job_id = ? AND state = 'NEEDS_MANUAL_OTP'", (jid,))
            db.set_job_status(conn, jid, "QUALIFIED", "retrying after an unanswered CAPTCHA or SMS code")
    stage(ws, log, args.stage_limit)
    if not args.no_send:
        send_all(ws, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
