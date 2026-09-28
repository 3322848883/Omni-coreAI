@echo off
rem windowless eth-range-paper
cd /d "%~dp0.."
set GATE_BOT_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
set OPENAI_API_KEY=sk-wb-Sm2NXyLm2rylSEQ7I_8HzNu_4pxmNfpoVtjcfyc9chM
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" plan-loop --bot eth-range-paper
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" paper-run --bot eth-range-paper
echo started eth-range-paper plan-loop + paper-run
