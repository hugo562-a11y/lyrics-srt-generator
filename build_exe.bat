@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv || goto :error
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\pyinstaller.exe" --noconfirm --windowed --name LyricsSrtGenerator --collect-all faster_whisper --collect-all ctranslate2 --collect-all PIL --hidden-import subtitle_png_renderer app.py
echo.
echo Build complete: dist\LyricsSrtGenerator\LyricsSrtGenerator.exe
pause
exit /b 0
:error
echo Failed to create the Python environment. Install Python 3.10+ and try again.
pause
