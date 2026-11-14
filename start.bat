@echo off
rem Lead Hub: double-click to start the web app and the monitors.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Ne najden .venv. Sozdajte ego: python -m venv .venv
    pause
    exit /b 1
)
if not exist ".env" (
    echo Ne najden fajl .env. Skopirujte .env.example v .env i zapolnite parol.
    pause
    exit /b 1
)

start "Lead Hub - monitors" cmd /k ".venv\Scripts\python.exe -m leadhub.monitor"
start "Lead Hub - server" cmd /k ".venv\Scripts\python.exe -m leadhub"

rem give the server a moment to bind before opening the browser
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8000
