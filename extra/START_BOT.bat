@echo off
title BTH-Master Bot Immortal Runner
setlocal

echo [%date% %time%] 🧹 Cleaning background processes...
taskkill /F /IM python.exe /T >nul 2>&1

:loop
echo [%date% %time%] 🚀 Starting bth_master_v90.py...
venv\Scripts\python.exe bth_master_v90.py
echo [%date% %time%] ⚠️ Bot stopped! Restarting in 5 seconds...
timeout /t 5
goto loop
