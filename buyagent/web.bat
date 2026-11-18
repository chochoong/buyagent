@echo off
REM buyagent own screen: http://127.0.0.1:8766 (double-click to open, close this window to stop)
chcp 65001 >nul
cd /d "%~dp0"
py -3 web.py %*
