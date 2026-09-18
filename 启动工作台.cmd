@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Python environment missing. Please install project dependencies first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "scripts\web_launcher.py"
