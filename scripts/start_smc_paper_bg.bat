@echo off
rem windowless smc-paper: plan-loop (LLM) + paper-run (exec+match)
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
start "" /b ".venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot smc-paper
start "" /b ".venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot smc-paper
echo started smc-paper plan-loop + paper-run (no window)
