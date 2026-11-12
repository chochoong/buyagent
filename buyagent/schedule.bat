@echo off
REM Register run.bat in Windows Task Scheduler (every hour). Run again to update.
REM   remove: schtasks /Delete /TN buyagent /F
chcp 65001 >nul
schtasks /Create /SC HOURLY /TN buyagent /TR "\"%~dp0run.bat\"" /F
if errorlevel 1 (echo FAILED & pause & exit /b 1)
echo.
echo OK: buyagent will run every hour. Log: %~dp0run.log
schtasks /Query /TN buyagent
pause
