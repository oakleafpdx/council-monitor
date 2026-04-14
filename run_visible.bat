@echo off
REM Council Meeting Monitor — Windows Scheduled Task Runner
REM This opens a visible PowerShell window so you can watch progress.
REM To switch to background mode later, change the scheduled task to run
REM "pythonw" instead of "python", or use run_background.bat.

cd /d "%~dp0"
echo ============================================================
echo   Council Meeting Monitor — Checking for new meetings...
echo   %date% %time%
echo ============================================================
echo.

python local_transcribe.py --check-new

echo.
echo ============================================================
echo   Done. This window will close in 30 seconds.
echo   (Or press any key to close now.)
echo ============================================================
timeout /t 30
