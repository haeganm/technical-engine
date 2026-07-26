@echo off
rem Restart wrapper. Uses the venv interpreter the README installs into, backs
rem off 30s between crashes, and STOPS on a config error (exit 2) instead of
rem relaunching a process that can never succeed.
cd /d %~dp0
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=py -3.12
:loop
%PY% -m engine run
if %ERRORLEVEL%==2 (
  echo Configuration error - fix .env and rerun. & exit /b 2
)
rem `timeout` fails under redirected stdin (Task Scheduler, piped logs); ping does not.
ping -n 31 127.0.0.1 >nul
goto loop
