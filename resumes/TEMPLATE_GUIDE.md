# Master CV template guide

There is one master template: `resumes/master_cv_template.docx`. Every tailored CV is a copy of it, so they all share the same single-column, ATS-safe layout.

## What the template controls
The template holds the page size and margins, and seven named paragraph styles:

| Style | Used for |
|---|---|
| CV Name | Your name at the top |
| CV Headline | The one-line headline under your name |
| CV Contact | Location, phone, email and links |
| CV Heading | Section headings (with the thin line under them) |
| CV Role | Job title or qualification, with dates on the right |
| CV Company | Company or school and location, in italics |
| CV Body | The summary paragraph |
| CV Bullet | Bullets, skill lines and certifications |

To change the look, open the template in Word, change these styles (Home > Styles > right-click > Modify), and save. Do not rename them. If you delete the file, `run_sprint.py` rebuilds the default one from `build_master_template.py`.

## What tailoring may change
- Which of your four tracks to use (AI data, software, mechanical, general), picked from the job text.
- The order of bullets inside each role, and of skills inside each group, so the best matches come first.
- How many bullets each role shows (5 at most).

## What tailoring may never change
- Any fact: companies, titles, dates, degrees, numbers or skills. Every line in a tailored CV must match a line in `config/master_profile.json` word for word. If it doesn't, the CV is not saved and an error says which line failed.
- The section order, fonts or layout.

## Adding or fixing facts
Edit `config/master_profile.json`. Each bullet has a `tracks` list saying which CV versions it appears in.
