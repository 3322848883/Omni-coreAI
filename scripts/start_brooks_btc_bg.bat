@echo off
rem windowless brooks-btc with LLM env injected (gw-flash provider)
cd /d "%~dp0.."
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if "%GATE_API_KEY%"=="" echo WARNING: GATE_API_KEY not set
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" supervisor --bot brooks-btc --auto-migrate
echo started brooks-btc (no window)
