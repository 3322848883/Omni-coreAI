@echo off
rem windowless wyckoff-paper: plan-loop (LLM) + paper-run (exec+match)
cd /d "%~dp0.."
set GATE_BOT_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" plan-loop --bot wyckoff-paper
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" paper-run --bot wyckoff-paper
echo started wyckoff-paper plan-loop + paper-run (no window)
