@echo off
rem Starts the league scheduler from the project folder (used by Windows Task Scheduler).
cd /d "%~dp0.."
if not exist data\logs mkdir data\logs
".venv\Scripts\python.exe" -m arena.main scheduler >> data\logs\scheduler-console.log 2>&1
