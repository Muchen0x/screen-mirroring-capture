@echo off
cd /d "%~dp0"
if not exist venv\ (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Failed to create venv. Make sure Python is installed and in PATH.
        pause
        exit /b 1
    )
    call venv\Scripts\activate
    pip install -e .
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
) else (
    call venv\Scripts\activate
)
python -m screen_mirroring_capture --gui
set "RC=%errorlevel%"
rem Exit code 3 means another instance was already running and was brought to
rem the front, so there is no output to read - don't leave a console waiting.
if "%RC%"=="3" exit /b 0
pause
exit /b %RC%
