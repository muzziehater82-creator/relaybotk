@echo off
cd /d "%~dp0"
echo Starting k! relay bot...
echo.
python -m pip install -q -r requirements.txt
python bot.py
echo.
echo ==========================================
echo  Bot stopped. Read any error above.
echo ==========================================
pause >nul
