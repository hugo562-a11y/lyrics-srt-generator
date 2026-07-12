@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First launch: creating a private Python environment...
  py -3 -m venv .venv || goto :error
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :error
exit /b 0
:error
echo.
echo The app could not start. Install Python 3.10+ from python.org, then try again.
pause
