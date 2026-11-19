# Job Sprint Agent

A free, local job application and recruiter email agent for a one-hour daily session on your laptop. It finds fresh jobs, tailors your CV for each one, fills in the application and stops before Submit so you approve every application yourself.

## Build status
| Step | What it adds | Status |
|---|---|---|
| 1 | Desktop folder, database, your profile, master CV template, CV tailoring, launchers | Done |
| 2 | Job discovery (last 24 hours only) and ranking by fit and low applicant count | Next |
| 3 | Tailored CV per job, form filling that stops before Submit, review page at http://127.0.0.1:8000 | Planned |
| 4 | Inbox scanning and recruiter reply drafts that you approve before sending | Planned |

## First run on Windows
1. Install Python 3.11 or newer from python.org if you haven't (tick "Add python.exe to PATH").
2. Double-click `START_SPRINT.bat` in this folder. The first run creates a private Python environment and installs what it needs.
3. It creates a `JobSprintAgent` folder on your Desktop and builds four sample CVs in `JobSprintAgent\tailored_cvs\_samples`. Open them and check every fact.

On macOS or Linux, run `./start_sprint.sh` instead.

## Commands
Run these from this folder, after the first run, with `.venv\Scripts\python.exe run_sprint.py ...` (or add the option after `START_SPRINT.bat`).

| Command | What it does |
|---|---|
| `run_sprint.py` | Daily sprint (for now: setup, checks and sample CVs) |
| `run_sprint.py --check` | Checks your profile and lists screening answers you still need to fill in |
| `run_sprint.py --sample-cvs` | Rebuilds the four sample CVs |
| `run_sprint.py --cv-for job.txt` | Builds a CV tailored to a job description saved in `job.txt` |

## Your profile
`config/master_profile.json` holds every fact the agent may use, taken from your four CVs. The agent never invents anything. Fill in the `screening_answers` that are `null` (notice period, salary, work authorisation and so on). Until you do, any form that asks them is flagged `NEEDS_USER_INPUT` for you to answer.

## Settings
Copy `.env.example` to `.env` and fill it in before steps 3 and 4: a free Gemini API key from https://aistudio.google.com/apikey and a Gmail App Password from https://myaccount.google.com/apppasswords. `.env` stays on your laptop and is never committed.

## Where things are
- This folder: the code.
- `Desktop\JobSprintAgent`: your data (database, logs, tailored CVs, screenshots, saved browser logins).

## Tests
`.venv\Scripts\python.exe -m pip install pytest` then `.venv\Scripts\python.exe -m pytest tests`
