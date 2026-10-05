@echo off
cd /d "%~dp0"
if exist secrets.bat call secrets.bat
python agent.py
pause
