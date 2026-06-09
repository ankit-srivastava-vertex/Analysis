@echo off
REM Run the Trading Charts Dashboard (Windows)
cd /d "%~dp0"
if exist "..\venv\Scripts\activate.bat" (
    call ..\venv\Scripts\activate.bat
)
pip install -q -r requirements.txt 2>nul
python app.py
pause
