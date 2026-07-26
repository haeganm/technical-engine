@echo off
rem Restart wrapper: if the scanner crashes, wait 30s and relaunch.
cd /d %~dp0
:loop
py -3.12 -m engine run
timeout /t 30 /nobreak >nul
goto loop
