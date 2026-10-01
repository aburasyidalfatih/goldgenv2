@echo off
title AutoPoster Gold Prospecting AI
echo ========================================================
echo   Auto Content Generator ^& Facebook AutoPoster (Gold AI)
echo ========================================================
echo.
echo Menyiapkan server lokal...
cd /d "%~dp0"
start http://127.0.0.1:8000
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
pause
