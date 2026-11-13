@echo off
REM Network Usage Tracker - Windows launch script
REM -------------------------------------------------------------
REM Installs dependencies on first run, then starts the dashboard.
REM Close the window (or click X) to stop monitoring and exit.
REM -------------------------------------------------------------

cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found in PATH.
    echo Install Python 3.9+ from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)

REM Install dependencies if missing
for /f "tokens=*" %%i in ('python -c "import psutil, customtkinter" 2^>^&1') do set IMPORT_FAIL=1
if defined IMPORT_FAIL (
    echo Installing dependencies (first run)...
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
)

echo Starting Network Usage Tracker...
start "" python app.py
exit /b 0
