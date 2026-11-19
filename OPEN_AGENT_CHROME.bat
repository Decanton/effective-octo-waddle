@echo off
REM Opens the Chrome profile the agent applies with (your copied "Dickens Owuor" profile once copied).
REM Close this window before the agent runs: only one Chrome can use the profile at a time.
cd /d "%~dp0"
".venv\Scripts\python.exe" run_sprint.py --open-chrome
