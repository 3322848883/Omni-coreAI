@echo off
rem windowless fangfangtu-paper: plan-loop (LLM) + paper-run (exec+match)
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data\bots\fangfangtu-paper\logs" mkdir "%CD%\data\bots\fangfangtu-paper\logs"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot fangfangtu-paper 1>>"%CD%\data\bots\fangfangtu-paper\logs\plan.out" 2>>"%CD%\data\bots\fangfangtu-paper\logs\plan.err"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot fangfangtu-paper 1>>"%CD%\data\bots\fangfangtu-paper\logs\paper.out" 2>>"%CD%\data\bots\fangfangtu-paper\logs\paper.err"
echo started fangfangtu-paper plan-loop + paper-run (no window)
