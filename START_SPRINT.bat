@echo off
REM One-click launcher for Windows. Double-click this file to run the daily sprint.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && py -3.11 --version >nul 2>nul && set "PY=py -3.11"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
    echo Python was not found. Install Python 3.11 or newer from python.org and tick "Add to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating a private Python environment. This takes a minute...
    %PY% -m venv .venv || (echo Could not create the environment. & pause & exit /b 1)
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (
    echo Installing requirements failed. Check your internet connection and try again.
    pause
    exit /b 1
)

if not exist ".venv\.playwright-ok" (
    echo Installing the browser used to fill forms. This happens once...
    ".venv\Scripts\python.exe" -m playwright install chromium && echo ok> ".venv\.playwright-ok"
)

".venv\Scripts\python.exe" run_sprint.py %*
echo.
pause
