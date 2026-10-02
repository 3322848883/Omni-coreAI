@echo off
rem 默认无窗口启动（pythonw + /b）。可选: start_all_bg.bat [bot_id]
cd /d "%~dp0.."
set OPENAI_BASE_URL=%OPENAI_BASE_URL%
if "%OPENAI_BASE_URL%"=="" set OPENAI_BASE_URL=https://api.deepseek.com/v1
if "%OPENAI_API_KEY%"=="" echo WARNING: OPENAI_API_KEY not set
if "%GATE_API_KEY%"=="" echo WARNING: GATE_API_KEY not set
if not exist "%CD%\data" mkdir "%CD%\data"
if "%~1"=="" (
start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" supervisor --auto-migrate 1>>"%CD%\data\supervisor.out" 2>>"%CD%\data\supervisor.err"
) else (
  start "" /b "%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" supervisor --bot %~1 --auto-migrate 1>>"%CD%\data\supervisor.out" 2>>"%CD%\data\supervisor.err"
)
echo started (no window)
