@echo off
rem windowless multi-persona: disc-test (weighted_vote + discussion)
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data\bots\disc-test\logs" mkdir "%CD%\data\bots\disc-test\logs"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" persona-run --group disc-test --interval 300 1>>"%CD%\data\bots\disc-test\logs\persona.out" 2>>"%CD%\data\bots\disc-test\logs\persona.err"
echo started multi-persona disc-test (weighted_vote + discussion)
