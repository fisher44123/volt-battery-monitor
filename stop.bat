@echo off
cd /d "%~dp0"
echo 正在停止 Volt 进程...
taskkill /F /IM Volt.exe /T 2>nul
taskkill /F /IM pythonw.exe /T 2>nul
echo 已停止。
timeout /t 1 >nul
exit
