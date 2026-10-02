@echo off
rem windowless orderflow-paper: plan-loop (LLM) + paper-run (exec+match)
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data\bots\orderflow-paper\logs" mkdir "%CD%\data\bots\orderflow-paper\logs"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot orderflow-paper 1>>"%CD%\data\bots\orderflow-paper\logs\plan.out" 2>>"%CD%\data\bots\orderflow-paper\logs\plan.err"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot orderflow-paper 1>>"%CD%\data\bots\orderflow-paper\logs\paper.out" 2>>"%CD%\data\bots\orderflow-paper\logs\paper.err"
echo started orderflow-paper plan-loop + paper-run (no window)
