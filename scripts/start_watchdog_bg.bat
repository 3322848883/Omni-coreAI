@echo off
rem windowless watchdog: auto-restart dead bot processes + Feishu notify
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data" mkdir "%CD%\data"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" watchdog --interval 15 1>>"%CD%\data\watchdog.out" 2>>"%CD%\data\watchdog.err"
echo started watchdog (check every 15s, Feishu notify via config/alerts.yaml)
