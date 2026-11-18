@echo off
rem Start on the Tailscale interface ONLY.
rem Reachable from your tailnet (phone/tablet with Tailscale on), invisible on Wi-Fi.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run run.bat once first to create the environment.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -u server.py --tailscale %*
pause
