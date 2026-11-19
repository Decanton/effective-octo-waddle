@echo off
REM Puts a "Job Agent Chrome" shortcut on your Desktop. Double-click once.
cd /d "%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Job Agent Chrome.lnk'); $s.TargetPath='%~dp0OPEN_AGENT_CHROME.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $c=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe' -ErrorAction SilentlyContinue).'(default)'; if($c){$s.IconLocation=$c+',0'}; $s.Save()"
echo The "Job Agent Chrome" shortcut is on your Desktop.
