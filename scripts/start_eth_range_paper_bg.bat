@echo off
rem windowless eth-range-paper
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
start "" /b ".venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot eth-range-paper
start "" /b ".venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" paper-run --bot eth-range-paper
echo started eth-range-paper plan-loop + paper-run
