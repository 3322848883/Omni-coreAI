@echo off
rem windowless smc-trader-paper (existing SMC live prompt on paper)
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data\bots\smc-trader-paper\logs" mkdir "%CD%\data\bots\smc-trader-paper\logs"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot smc-trader-paper 1>>"%CD%\data\bots\smc-trader-paper\logs\plan.out" 2>>"%CD%\data\bots\smc-trader-paper\logs\plan.err"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot smc-trader-paper 1>>"%CD%\data\bots\smc-trader-paper\logs\paper.out" 2>>"%CD%\data\bots\smc-trader-paper\logs\paper.err"
echo started smc-trader-paper plan-loop + paper-run
