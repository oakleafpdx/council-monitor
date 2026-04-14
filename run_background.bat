@echo off
REM Council Meeting Monitor — Background Mode (no visible window)
REM Switch the scheduled task to use this file when you're ready.

cd /d "%~dp0"
python local_transcribe.py --check-new >> data\log.txt 2>&1
