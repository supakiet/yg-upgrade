@echo off
:: ขอสิทธิ์ Administrator อัตโนมัติ (เกมจะรับคลิกจากโปรแกรมได้)
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
python upgrade_gui.py
if %errorlevel% neq 0 pause
