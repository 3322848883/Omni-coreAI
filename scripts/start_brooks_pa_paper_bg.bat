@echo off
rem windowless brooks-pa-paper (existing Brooks live prompt on paper)
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data\bots\brooks-pa-paper\logs" mkdir "%CD%\data\bots\brooks-pa-paper\logs"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot brooks-pa-paper 1>>"%CD%\data\bots\brooks-pa-paper\logs\plan.out" 2>>"%CD%\data\bots\brooks-pa-paper\logs\plan.err"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot brooks-pa-paper 1>>"%CD%\data\bots\brooks-pa-paper\logs\paper.out" 2>>"%CD%\data\bots\brooks-pa-paper\logs\paper.err"
echo started brooks-pa-paper plan-loop + paper-run
