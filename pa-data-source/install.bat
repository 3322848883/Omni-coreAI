@echo off
REM =============================================================================
REM pa-data-source Windows one-click setup (mirror of Linux server_setup.sh)
REM Usage: double-click install.bat or run from command line
REM Steps: 1. detect Python 3.9+
REM        2. create virtual env .venv and install deps (pyyaml websocket-client)
REM        3. print next steps
REM Full docs: README.md
REM =============================================================================
setlocal
cd /d "%~dp0"

echo [1/3] Detecting Python 3.9+ ...
set "PYCMD=python"
py -3 --version >nul 2>&1
if %errorlevel%==0 set "PYCMD=py -3"
%PYCMD% --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.9+ and check "Add python.exe to PATH".
  echo         Download: https://www.python.org/downloads/
  exit /b 1
)

echo [2/3] Creating virtual env .venv and installing deps ...
if not exist ".venv" %PYCMD% -m venv .venv
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Dependency install failed. Check network and retry.
  exit /b 1
)

echo [3/3] Done.
echo ------------------------------------------------------------
echo Start (set keys via env vars, not stored in files):
echo   set GATE_API_KEY=your_api_key
echo   set GATE_API_SECRET=your_api_secret
echo   .venv\Scripts\python.exe watchdog.py
echo Verify:
echo   curl http://127.0.0.1:18080/health
echo Note: GUI launcher launcher.vbs uses system Python; if using venv,
echo       activate venv first or pip install -r requirements.txt into system Python.
echo ------------------------------------------------------------
endlocal
