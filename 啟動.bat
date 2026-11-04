@echo off
cd /d "%~dp0"
title Campus Survey Helper
"runtime\python\python.exe" app.py
echo.
echo Stopped. Press any key to close this window.
pause >nul
