@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在以调试模式启动 UniBattery 控制中心...
python main.py
pause
