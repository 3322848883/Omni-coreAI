@echo off
rem 默认无窗口启动（pythonw + /b）。可选: start_all_bg.bat [bot_id]
cd /d "%~dp0.."
set OPENAI_BASE_URL=%OPENAI_BASE_URL%
if "%OPENAI_BASE_URL%"=="" set OPENAI_BASE_URL=https://api.deepseek.com/v1
if "%OPENAI_API_KEY%"=="" echo WARNING: OPENAI_API_KEY not set
if "%GATE_API_KEY%"=="" echo WARNING: GATE_API_KEY not set
if "%~1"=="" (
  start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" supervisor --auto-migrate
) else (
  start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" supervisor --bot %~1 --auto-migrate
)
echo started (no window)
