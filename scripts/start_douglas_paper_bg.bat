@echo off
rem windowless douglas-paper: psychology/discipline layer (hold-only)
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
start "" /b ".venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" plan-loop --bot douglas-paper
echo started douglas-paper plan-loop (hold-only, no paper-run needed)
