@echo off
rem Double-click to start Lead Hub (site + order search) in this window.
chcp 65001 >nul
title Lead Hub
cd /d "%~dp0"
".venv\Scripts\python.exe" -m leadhub.run
echo.
echo Lead Hub stopped. Press any key to close.
pause >nul
