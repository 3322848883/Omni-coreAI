@echo off
rem 真后台启动 brooks-btc（pythonw，无控制台窗口）
cd /d "%~dp0.."
set OPENAI_BASE_URL=https://api.deepseek.com/v1
if "%OPENAI_API_KEY%"=="" echo WARNING: OPENAI_API_KEY not set
if "%GATE_API_KEY%"=="" echo WARNING: GATE_API_KEY not set
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" supervisor --bot brooks-btc --auto-migrate
echo brooks-btc supervisor started (pythonw, no window)
echo logs: logs\supervisor.err  data\bots\brooks-btc\logs\
