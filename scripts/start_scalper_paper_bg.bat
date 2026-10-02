@echo off
rem windowless scalper-paper
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
start "" /b ".venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot scalper-paper
start "" /b ".venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot scalper-paper
echo started scalper-paper plan-loop + paper-run
