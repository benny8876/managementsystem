@echo off
rem Removes the Shwe Muse server program (data and backups are kept).
cd /d "%~dp0"
fltmc >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0setup\uninstall.ps1"
