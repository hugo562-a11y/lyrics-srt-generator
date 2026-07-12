@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call run.bat
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller
".venv\Scripts\pyinstaller.exe" --noconfirm --windowed --name LyricsSrtGenerator --collect-all faster_whisper --collect-all ctranslate2 app.py
echo.
echo Build complete: dist\LyricsSrtGenerator\LyricsSrtGenerator.exe
pause
