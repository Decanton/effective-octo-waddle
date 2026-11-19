# Job Sprint Agent

A free, local job application and recruiter email agent for a one-hour daily session on your laptop. It finds fresh jobs, tailors your CV for each one, fills in the application and stops before Submit so you approve every application yourself.

## Build status
| Step | What it adds | Status |
|---|---|---|
| 1 | Desktop folder, database, your profile, master CV template, CV tailoring, launchers | Done |
| 2 | Job discovery (last 24 hours only) and ranking by fit and low applicant count | Done |
| 3 | Tailored CV per job, form filling that stops before Submit, review page at http://127.0.0.1:8000 | Done |
| 4 | Inbox scanning and recruiter reply drafts that you approve before sending | Next |

## First run on Windows
1. Install Python 3.11 or newer from python.org if you haven't (tick "Add python.exe to PATH").
2. Double-click `START_SPRINT.bat` in this folder. The first run creates a private Python environment and installs what it needs.
3. It creates a `JobSprintAgent` folder on your Desktop and builds four sample CVs in `JobSprintAgent\tailored_cvs\_samples`. Open them and check every fact.

On macOS or Linux, run `./start_sprint.sh` instead.

## Commands
Run these from this folder, after the first run, with `.venv\Scripts\python.exe run_sprint.py ...` (or add the option after `START_SPRINT.bat`).

| Command | What it does |
|---|---|
| `run_sprint.py` | Daily sprint: profile check, find and rank jobs, fill forms (stopping before Submit), then open the review page |
| `run_sprint.py --login-setup` | Opens the saved browser so you can sign in to LinkedIn, AfterQuery and others once |
| `run_sprint.py --stage-only --stage-limit 5` | Fills forms for the top 5 qualified jobs, without submitting |
| `run_sprint.py --review` | Opens the review page at http://127.0.0.1:8000 |
| `run_sprint.py --discover` | Finds jobs posted in the last 24 hours, ranks them and saves a CSV in `JobSprintAgent\exports` |
| `run_sprint.py --mark-applied AfterQuery` | Marks an AI platform as applied so it stops being listed |
| `run_sprint.py --check` | Checks your profile and lists screening answers you still need to fill in |
| `run_sprint.py --sample-cvs` | Rebuilds the four sample CVs |
| `run_sprint.py --cv-for job.txt` | Builds a CV tailored to a job description saved in `job.txt` |

## How jobs are found and ranked
- **Sources:** free remote-job boards with exact posting times (Remotive, RemoteOK, Himalayas, Jobicy, We Work Remotely), LinkedIn through jobspy (Kenya, plus remote roles worldwide; it stops for the day if LinkedIn starts blocking), and the public Greenhouse, Lever and Ashby boards listed in `config/ai_platforms.yaml`. Edit searches in `config/search_queries.yaml`.
- **24-hour rule:** enforced in code (`src/discovery/freshness.py`). Undated or older jobs are dropped. LinkedIn searches only return the last 24 hours, and for the best matches the agent opens the public job page to read the exact age and applicant count.
- **Knockouts:** titles like Director or Intern, descriptions requiring US citizenship or clearance, onsite jobs outside Kenya, and remote jobs restricted to the US, UK or EU.
- **Ranking:** 55% fit with your profile, 20% how recent, 25% how few applicants. An unknown applicant count is neutral. Weights live in `master_profile.json` under `ranking_weights`.
- **AI platforms** with no dated postings (AfterQuery, Outlier, Alignerr, DataAnnotation, Mercor) are listed each run until you mark them applied.

## Applying: what happens and what never happens
- For each top job the agent builds a tailored CV, opens the real application form (Greenhouse, Lever or Ashby, including ones linked from remote job boards), fills what it can from your profile, uploads the CV and takes a screenshot. It **never presses Submit** while staging.
- Questions your profile can't answer are flagged **needs you**. Demographic questions get "decline to self-identify" when offered. Consent and privacy boxes are never ticked for you: you tick them on the review page.
- Written questions ("Why do you want to work here?") get a short AI draft from Gemini or Groq using only your profile. It is marked **AI draft** for you to read and edit.
- On the review page, **Submit** reopens the form, fills it again with your edits, and presses Submit in a visible browser so you can solve any CAPTCHA. **Skip** drops the job. Jobs with no supported form show **Open application** and your tailored CV to apply yourself.

## Your profile
`config/master_profile.json` holds every fact the agent may use, taken from your four CVs. The agent never invents anything. Fill in the `screening_answers` that are `null` (notice period, salary, work authorisation and so on). Until you do, any form that asks them is flagged `NEEDS_USER_INPUT` for you to answer.

## Settings
Copy `.env.example` to `.env` and fill it in before steps 3 and 4: a free Gemini API key from https://aistudio.google.com/apikey and a Gmail App Password from https://myaccount.google.com/apppasswords. `.env` stays on your laptop and is never committed.

## Where things are
- This folder: the code.
- `Desktop\JobSprintAgent`: your data (database, logs, tailored CVs, screenshots, saved browser logins).

## Tests
`.venv\Scripts\python.exe -m pip install pytest` then `.venv\Scripts\python.exe -m pytest tests`
