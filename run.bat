@echo off
setlocal
cd /d "%~dp0"
py -3 app.py 2>nul || python app.py
if errorlevel 1 goto :error
exit /b 0
:error
echo.
echo The app could not start. Install Python 3.10+ from python.org, then try again.
pause
