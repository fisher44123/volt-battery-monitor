@echo off
cd /d "%~dp0"
echo Starting Volt in background...
start "" pythonw main.py
exit
