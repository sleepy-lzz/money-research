@echo off
cd /d "%~dp0"
if not exist "runtime\daily-lab\latest.html" (
    echo No daily report yet. Run the daily-lab check first.
    pause
    exit /b 1
)
start "" "runtime\daily-lab\latest.html"
