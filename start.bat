@echo off
rem Starts Bird Monitoring in its own window (or the browser if pywebview is missing).
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" desktop\launcher.py %*
) else (
  python desktop\launcher.py %*
)
