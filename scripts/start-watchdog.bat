@echo off
rem Start gate-signal-bot under watchdog (crash auto-restart).
rem Optional: set GATE_API_KEY / GATE_API_SECRET (or GATE_TESTNET_*) before start.
cd /d "%~dp0.."
set PYTHONPATH=%CD%
".venv\Scripts\python.exe" scripts\watchdog.py %*
