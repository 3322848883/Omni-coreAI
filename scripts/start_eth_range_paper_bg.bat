@echo off
rem windowless eth-range-paper
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data\bots\eth-range-paper\logs" mkdir "%CD%\data\bots\eth-range-paper\logs"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot eth-range-paper 1>>"%CD%\data\bots\eth-range-paper\logs\plan.out" 2>>"%CD%\data\bots\eth-range-paper\logs\plan.err"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot eth-range-paper 1>>"%CD%\data\bots\eth-range-paper\logs\paper.out" 2>>"%CD%\data\bots\eth-range-paper\logs\paper.err"
echo started eth-range-paper plan-loop + paper-run
