@echo off
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -B -m gym_tracker.automation stop
pause
