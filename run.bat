@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3 app.py
    goto :check
)
where python >nul 2>&1
if %errorlevel% equ 0 (
    python app.py
    goto :check
)
goto :no_python

:check
if %errorlevel% neq 0 goto :no_python
exit /b 0

:no_python
echo.
echo ============================================
echo  找不到 Python，請先安裝 Python 3.10+
echo  下載網址：https://www.python.org/downloads/
echo  安裝時請勾選 "Add Python to PATH"
echo ============================================
pause
