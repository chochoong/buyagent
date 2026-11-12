@echo off
REM buyagent: run once and append output to run.log (used by Task Scheduler)
REM   run.bat            normal run
REM   run.bat --demo     demo data
chcp 65001 >nul
cd /d "%~dp0"
REM keep run.log under 2MB (older output moves to run.old.log)
if exist run.log for %%F in (run.log) do if %%~zF GTR 2000000 move /y run.log run.old.log >nul
py -3 main.py %* >> run.log 2>&1
