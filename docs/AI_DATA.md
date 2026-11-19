# What the AI services see

The agent runs on your laptop. It uses an AI model for three jobs only, and sends only what each job needs.
Choose where the AI runs with `llm.mode` in `config/agent_settings.yaml`:

| mode | where the text goes |
|---|---|
| `cloud` (default) | Google Gemini first, then Groq, then Ollama on this laptop if both are unavailable |
| `local_only` | Ollama on this laptop only. Nothing leaves the laptop. |
| `off` | No AI. Template cover letters; written questions wait for you. |

## What is sent, per job

| task | sent to the AI | never sent |
|---|---|---|
| Cover letter (email applications) | your name, city, phone, email, portfolio link, summary, work history, education and certifications from `master_profile.json`; the job title, company and up to 5,000 characters of the job post | your street address, salary, passwords, `.env` keys, other jobs |
| Written answers on forms ("Why do you want this role?") | the question; job title, company and up to 3,000 characters of the post; your name, city, summary, work history, education and certifications | contact details, salary, work-authorisation and other screening answers (those are filled from your profile, not by the AI), passwords |
| Recruiter replies | your name, email, phone, city, LinkedIn, portfolio, summary and your screening answers (notice period, salary expectations, work authorisation), so a reply can answer "when can you start?"; the recruiter's name, address, subject and up to 4,000 characters of their email | other emails in your inbox, attachments, passwords |

Never sent anywhere: `.env` (API keys, Gmail app password), portal passwords (stored encrypted with Windows DPAPI
in the local database), your Google password (the agent never sees it), screenshots and the database itself.

## The providers' own rules

- **Gemini (free tier):** Google may use free-tier prompts to improve its products. Use `local_only` if that matters to you.
- **Groq:** says it does not train on API inputs.
- **Ollama:** runs entirely on your laptop.

Answers that contain numbers or claims not found in your profile are flagged `[FILL IN ...]` and never sent automatically.
