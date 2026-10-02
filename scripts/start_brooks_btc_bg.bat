@echo off
rem windowless brooks-btc with LLM env injected (gw-flash provider)
cd /d "%~dp0.."
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if "%GATE_API_KEY%"=="" echo WARNING: GATE_API_KEY not set
if not exist "%CD%\data" mkdir "%CD%\data"
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" supervisor --bot brooks-btc --auto-migrate 1>>"%CD%\data\supervisor-brooks-btc.out" 2>>"%CD%\data\supervisor-brooks-btc.err"
echo started brooks-btc (no window)
