@echo off
rem windowless watchdog: auto-restart dead bot processes + Feishu notify
cd /d "%~dp0.."
set GATE_BOT_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
set OPENAI_API_KEY=sk-wb-Sm2NXyLm2rylSEQ7I_8HzNu_4pxmNfpoVtjcfyc9chM
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" watchdog --interval 15
echo started watchdog (check every 15s, Feishu notify via config/alerts.yaml)
