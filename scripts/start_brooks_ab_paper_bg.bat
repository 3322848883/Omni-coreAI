@echo off
rem windowless brooks-ab-paper: plan-loop (LLM) + paper-run (exec+match)
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
set OMNIALPHA_LOCK_HELD=0
if not exist "%CD%\data\bots\brooks-ab-paper\logs" mkdir "%CD%\data\bots\brooks-ab-paper\logs"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot brooks-ab-paper 1>>"%CD%\data\bots\brooks-ab-paper\logs\plan.out" 2>>"%CD%\data\bots\brooks-ab-paper\logs\plan.err"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot brooks-ab-paper 1>>"%CD%\data\bots\brooks-ab-paper\logs\paper.out" 2>>"%CD%\data\bots\brooks-ab-paper\logs\paper.err"
echo started brooks-ab-paper plan-loop + paper-run (no window)
