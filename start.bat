@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Project environment missing. Follow README.md to install dependencies.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 (
    echo VJVision failed to start. See the error above.
    pause
    exit /b 1
)
