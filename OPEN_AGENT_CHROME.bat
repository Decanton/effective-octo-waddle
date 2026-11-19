@echo off
REM Opens the agent's own Chrome profile (the one it applies with), as a normal Chrome window.
REM Sign in to Google here once; the agent then uses "Continue with Google" on job sites.
REM Close this window before the agent runs: only one Chrome can use the profile at a time.
setlocal
set "PROFILE=%USERPROFILE%\Desktop\JobSprintAgent\browser_profile"
if defined JOBSPRINT_DESKTOP_DIR set "PROFILE=%JOBSPRINT_DESKTOP_DIR%\JobSprintAgent\browser_profile"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" ( echo Google Chrome was not found. & pause & exit /b 1 )
start "" "%CHROME%" --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check "%~1"
