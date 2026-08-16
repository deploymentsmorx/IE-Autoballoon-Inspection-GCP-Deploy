@echo off
cd /d "%~dp0"
where npm >nul 2>&1
if errorlevel 1 (
  echo npm not found. Install Node.js first.
  pause
  exit /b 1
)
echo Starting SmorX web app at http://127.0.0.1:9835
echo Keep this window open while using the browser.
npm run dev
