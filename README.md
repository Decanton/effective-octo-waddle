# Job Sprint Agent

A free, local job application and recruiter email agent for a one-hour daily session on your laptop. It finds fresh jobs, tailors your CV for each one, fills in the application and stops before Submit so you approve every application yourself.

## Build status
| Step | What it adds | Status |
|---|---|---|
| 1 | Desktop folder, database, your profile, master CV template, CV tailoring, launchers | Done |
| 2 | Job discovery (last 48 hours) and ranking by fit and low applicant count | Done |
| 3 | Tailored CV per job, form filling that stops before Submit, review page at http://127.0.0.1:8000 | Done |
| 4 | Automatic sending (daily cap), sign-in once per site, Gmail job alerts and recruiter reply drafts, daily scheduled run | Done |

## First run on Windows
1. Install Python 3.11 or newer from python.org if you haven't (tick "Add python.exe to PATH").
2. Double-click `START_SPRINT.bat` in this folder. The first run creates a private Python environment and installs what it needs.
3. It creates a `JobSprintAgent` folder on your Desktop and builds four sample CVs in `JobSprintAgent\tailored_cvs\_samples`. Open them and check every fact.

On macOS or Linux, run `./start_sprint.sh` instead.

## Commands
Run these from this folder, after the first run, with `.venv\Scripts\python.exe run_sprint.py ...` (or add the option after `START_SPRINT.bat`).

| Command | What it does |
|---|---|
| `run_sprint.py` | Daily sprint: read the inbox, find and rank jobs, fill forms, send the complete ones (see Automatic sending), then open the review page |
| `run_sprint.py --no-send` | Same, but sends nothing this time |
| `run_sprint.py --send-ready` | Sends the complete applications now, with the same rules |
| `run_sprint.py --email-only` | Reads the inbox only: job alerts go into the job list, recruiter emails get a draft reply |
| `INSTALL_DAILY_RUN.bat` | Double-click once to run the sprint by itself every day at a time you choose |
| `run_sprint.py --login-setup` | Opens the saved browser so you can sign in (or sign up) once to LinkedIn, Himalayas and every site where a job was waiting for a sign-in |
| `run_sprint.py --restage --stage-only` | Tries again the jobs that needed a sign-in or had no form |
| `run_sprint.py --stage-only --stage-limit 5` | Fills forms for the top 5 qualified jobs, without submitting |
| `run_sprint.py --review` | Opens the review page at http://127.0.0.1:8000 |
| `run_sprint.py --discover` | Finds jobs posted in the last 48 hours, ranks them and saves a CSV in `JobSprintAgent\exports` |
| `run_sprint.py --mark-applied AfterQuery` | Marks an AI platform as applied so it stops being listed |
| `run_sprint.py --check` | Checks your profile and lists screening answers you still need to fill in |
| `run_sprint.py --sample-cvs` | Rebuilds the four sample CVs |
| `run_sprint.py --cv-for job.txt` | Builds a CV tailored to a job description saved in `job.txt` |

## How jobs are found and ranked
- **Sources:** free remote-job boards with exact posting times (Remotive, RemoteOK, Himalayas, Jobicy, We Work Remotely), LinkedIn through jobspy (Kenya, plus remote roles worldwide; it stops for the day if LinkedIn starts blocking), and the public Greenhouse, Lever and Ashby boards listed in `config/ai_platforms.yaml`. Edit searches in `config/search_queries.yaml`.
- **48-hour rule:** enforced in code (`src/discovery/freshness.py`, `max_age_hours` in `config/search_queries.yaml`). Undated or older jobs are dropped. LinkedIn searches only return the last 48 hours, and for the best matches the agent opens the public job page to read the exact age and applicant count. AI training roles get a priority boost (`ranking_weights.track_bonus` in `master_profile.json`).
- **Knockouts:** titles like Director or Intern, descriptions requiring US citizenship or clearance, onsite jobs outside Kenya, and remote jobs restricted to the US, UK or EU.
- **Ranking:** 55% fit with your profile, 20% how recent, 25% how few applicants. An unknown applicant count is neutral. Weights live in `master_profile.json` under `ranking_weights`.
- **AI platforms** with no dated postings (AfterQuery, Outlier, Alignerr, DataAnnotation, Mercor) are listed each run until you mark them applied.

## Applying: what happens and what never happens
- For each top job the agent builds a tailored CV, opens the real application form (Greenhouse, Lever or Ashby, including ones linked from remote job boards), fills what it can from your profile, uploads the CV and takes a screenshot. Staging never presses Submit. Sending happens afterwards, under the rules in Automatic sending.
- Questions your profile can't answer are flagged **needs you**. Demographic questions get "decline to self-identify" when offered. Consent and privacy boxes are ticked only if you allow it in `config/agent_settings.yaml`. Otherwise you tick them on the review page.
- Written questions ("Why do you want to work here?") get a short AI draft from Gemini or Groq using only your profile. It is marked **AI draft** for you to read and edit. What each AI service sees, and how to keep everything on the laptop with Ollama (`llm.mode: local_only`), is in [docs/AI_DATA.md](docs/AI_DATA.md).
- On the review page, **Submit** reopens the form, fills it again with your edits, and presses Submit in a visible browser so you can solve any CAPTCHA. **Skip** drops the job. Jobs with no supported form show **Open application** and your tailored CV to apply yourself.

## Automatic sending
Settings live in `config/agent_settings.yaml`. With `auto_submit.enabled: true` the daily run sends an application by itself only when:
- every required question is answered and your tailored CV is attached;
- the form is on Greenhouse, Lever or Ashby (`only_hosts`);
- there's no CAPTCHA;
- there's no required consent box, unless you set `accept_required_consents: true`;
- fewer than `max_per_day` applications went out today.

Everything else waits on the review page, where **Submit All Verified Jobs** submits every verified form at once (each is checked again first). A form where Submit was pressed but no thank-you page appeared is marked **sent? check** and is never sent twice.

## Applying by email
Some jobs say "send your CV and cover letter to hr@company.com". The agent finds that address in the job post, or on the page the Apply link opens. It then writes a cover letter that uses only facts from your profile, and emails it with your tailored CV (both as Word files) from `EMAIL_ADDRESS`. It never uses privacy, accessibility, support or no-reply addresses. Each job is emailed once. Set `email_applications` in `config/agent_settings.yaml` (on, with up to 10 a day) to choose whether these go out automatically or wait on the review page, where they show as **by email** with the letter editable.

## Sign-ins and Continue with Google
The agent uses your installed Google Chrome with its own profile (`JobSprintAgent\browser_profile`). Your everyday Chrome profile can't be shared with an automated browser, so sign in to Google once there:
1. Run `START_SPRINT.bat --login-setup`, or press **Sign in to sites** on the review page. A normal Chrome window opens.
2. On the first tab, sign in to Google as your job-search Gmail (`EMAIL_ADDRESS`). Use your password or phone prompt; the agent never sees it.
3. On the other tabs, use **Continue with Google** on each job site, then close the window.

After that, whenever a site asks for an account, the agent clicks its **Continue with Google** button and picks your account by itself (`google_sign_in` in `config/agent_settings.yaml`). If Google asks for a password or a phone prompt, the job waits for you as **sign in once**.

## Email
With `EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` in `.env`, each run reads the last 48 hours of the inbox. It opens the inbox read-only, so nothing is marked read or moved.
- **Job alerts** (LinkedIn, Indeed, Glassdoor, Himalayas, BrighterMonday and others) become jobs in the normal list.
- **Recruiter emails** show on the **Recruiter emails** tab with a draft reply. Replies go out only when you press **Send reply**.

## Your profile
`config/master_profile.json` holds every fact the agent may use, taken from your four CVs. The agent never invents anything. Fill in the `screening_answers` that are `null` (notice period, salary, work authorisation and so on). Until you do, any form that asks them is flagged `NEEDS_USER_INPUT` for you to answer.

## Settings
Copy `.env.example` to `.env` and fill it in before steps 3 and 4: a free Gemini API key from https://aistudio.google.com/apikey and a Gmail App Password from https://myaccount.google.com/apppasswords. `.env` stays on your laptop and is never committed.

## Where things are
- This folder: the code.
- `Desktop\JobSprintAgent`: your data (database, logs, tailored CVs, screenshots, saved browser logins).

## Tests
`.venv\Scripts\python.exe -m pip install pytest` then `.venv\Scripts\python.exe -m pytest tests`

## Company career sites, portal accounts and the final check

- **Job Agent Chrome over a local debugging port.** The agent attaches to the Job Agent Chrome window
  (the desktop shortcut starts it with `--remote-debugging-port=9222`, reachable only from this laptop),
  so it shares that window's Google sign-in and cookies. It works in its own tab and closes only tabs it opened.
- **Apply buttons that open a new tab.** When "Apply" opens the company's site in a new tab, the agent
  switches to that tab, fills the form there and attaches the tailored CV.
- **Portal accounts.** If a career site needs an account and has no "Continue with Google", the agent signs
  up with the job-search Gmail and a strong generated password. The password is stored encrypted
  (Windows DPAPI) in the `portal_credentials` table and reused next time. It then waits up to 60 seconds for
  the site's verification code or link in Gmail and completes it. It never solves a CAPTCHA, never ticks
  marketing boxes, and stops if a phone check is required.
- **Strict check before "verified ready".** A form is only marked verified when every required field is
  filled, the CV (.docx or .pdf) is attached and no error message is showing. Anything else stays in
  "Needs you" with the reason.
- **Submit All Verified Jobs.** One button on the review page re-checks and submits every verified form,
  waits for a confirmation message or page, and saves a receipt (page, confirmation text, time, screenshot).
  Forms without a clear confirmation are marked "sent, not confirmed" and are never sent twice.

## CAPTCHAs and phone codes

The agent never solves a CAPTCHA. When a CAPTCHA or a code texted to your phone blocks a job:

- If **Google Messages for Web** is open in Job Agent Chrome (it opens automatically; pin the tab once),
  the agent reads the new code from your phone's texts and enters it. Only a new message that looks like a
  verification code is used; M-PESA, bank and balance messages are ignored.
- Otherwise the exact Chrome tab comes to the front with a desktop alert and a chime:
  `[JobSprintAgent] Action needed on <Company>: Solve the CAPTCHA in Chrome`. Solve it (or type the code)
  and the agent carries on with the same form. After 2 minutes with no answer the job is marked
  `NEEDS_MANUAL_OTP` and the run moves on; the next run tries it again.
- The agent works at a steady pace between form steps (`human_assist.pacing`), so sites aren't hammered.

## Updates

The agent installs new versions from GitHub by itself (`update:` in `config/agent_settings.yaml`): before
each daily run, and every hour while the review page is open. The tests run on the new version first; if any
fail, nothing changes. Your `.env`, `.venv` and data are never touched, and the last 3 versions are kept in
`_backups`. The review page at http://127.0.0.1:8000 stays online: the new version starts next to the old
one and takes over once it answers; anything in progress finishes on the old one. You can also press
**Check for updates** on the review page, or run `START_SPRINT.bat --update`.

## Seeing what happened

- **Why this job** (on each application): which of the post's requirements your CV covers, which it doesn't, and the scores.
- **Application record**: the job post as it was when the agent applied, the answers given, the CV sent, the receipt, replies and follow-ups.
- **CV changes for this job**: your standard CV next to the one sent, with changed lines highlighted.
- **Results** tab: found → good fit → filled → sent → replied → interviews → offers, response rate per source, and every application's status.
  Email applications with no answer after 7 days get one polite follow-up in the same email thread (`follow_ups:` in the settings).
- **Activity log** (Results tab): every email, form, reply, follow-up, site account and update the agent made.
- Reposts of a job you already applied to are skipped; jobs not filled within 2 days of being found are dropped.
- At most 5 forms a day go to any one site (`auto_submit.per_site_default`).
- Keys: ↑/↓ or j/k to move, Enter to submit, Backspace to skip, / to search.

## LinkedIn Easy Apply

With `linkedin_easy_apply.enabled: true` (on, up to 40 a day) and LinkedIn signed in inside Job Agent Chrome
(the agent tries "Continue with Google" once if it isn't), LinkedIn jobs are applied to right there:
every Easy Apply step is filled from your profile, the tailored CV is uploaded, "Follow company" is unticked and
the application is submitted, with a receipt saved. "Apply on company site" jobs are followed to the employer's form.
If LinkedIn asks something the agent can't answer, the draft is discarded and the question shows on the review page.
A LinkedIn security check is never solved: that job is skipped. LinkedIn may restrict accounts it thinks are automated.
