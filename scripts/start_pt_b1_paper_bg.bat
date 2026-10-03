@echo off
rem windowless pt-b1: paper-run only（纯执行账户，分析由 persona-run --group disc-exp 驱动）
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data\bots\pt-b1\logs" mkdir "%CD%\data\bots\pt-b1\logs"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot pt-b1 1>>"%CD%\data\bots\pt-b1\logs\paper.out" 2>>"%CD%\data\bots\pt-b1\logs\paper.err"
echo started pt-b1 paper-run (no window)
