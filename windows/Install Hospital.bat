@echo off
rem Hospital site server (hospital / clinic) - one-click installer. Double-click this file.
cd /d "%~dp0"
fltmc >nul 2>&1
if %errorlevel% neq 0 (
    rem Ask Windows for administrator rights, then run this file again.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0setup\install.ps1"
