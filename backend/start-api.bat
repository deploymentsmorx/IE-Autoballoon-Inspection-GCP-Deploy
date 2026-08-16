@echo off
title AutoBalloon API (port 9686)
cd /d "%~dp0"
echo.
echo  ============================================
echo   AutoBalloon API
echo   Keep this window OPEN while using the app
echo   http://127.0.0.1:9686
echo  ============================================
echo.
"C:\Users\AIMLE\AppData\Local\Programs\Python\Python310\python.exe" -m uvicorn app.main:app --port 9686 --host 127.0.0.1
echo.
echo  API stopped. Press any key to close.
pause
