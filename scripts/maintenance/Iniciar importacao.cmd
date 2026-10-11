@echo off
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente Python ausente. Execute uv sync --locked --all-groups.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -m gym_tracker.automation start
pause
