@echo off
REM Makes the agent run by itself every day. Double-click once.
REM It runs the full sprint (inbox, job search, fill forms, send complete ones)
REM at the time below while you are signed in to Windows, without opening the review page.
REM To change the time, run this again. To stop it: schtasks /delete /tn JobSprintAgent /f
setlocal
cd /d "%~dp0"
set "RUNAT=%~1"
if "%RUNAT%"=="" set /p RUNAT=What time should it run every day (24-hour, e.g. 07:30)? 
if "%RUNAT%"=="" set "RUNAT=07:30"
schtasks /create /tn "JobSprintAgent" /sc daily /st %RUNAT% /f /tr "\"%~dp0START_SPRINT.bat\" --no-review"
call "%~dp0MAKE_SHORTCUT.bat" >nul 2>nul
if errorlevel 1 (
    echo Could not create the daily task.
) else (
    echo Done. The agent will run every day at %RUNAT%. Open the review page any time with START_SPRINT.bat --review
)
pause
