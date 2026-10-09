@echo off
title AI Trading League
rem Double-click this file to start the AI Trading League.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First start: setting things up. This takes a few minutes...
  python -m venv .venv
  if errorlevel 1 (
    echo.
    echo Python was not found. Install Python 3.12 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during the install. Then double-click start.cmd again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Installing the Python packages failed. See the messages above.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m arena.main app
pause
