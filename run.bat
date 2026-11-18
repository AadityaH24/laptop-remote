@echo off
rem Start the Laptop Remote server. Creates the venv on first run.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating virtual environment...
  py -3 -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :fail
  echo Optional extras ^(now-playing card, QR code^)...
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements-optional.txt
  echo.
)

".venv\Scripts\python.exe" -u server.py %*
pause
exit /b 0

:fail
echo.
echo Setup failed. Check the messages above.
pause
exit /b 1
