@echo off
rem windowless larry-paper: plan-loop (LLM) + paper-run (exec+match)
cd /d "%~dp0.."
set GATE_BOT_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
set OPENAI_API_KEY=sk-wb-Sm2NXyLm2rylSEQ7I_8HzNu_4pxmNfpoVtjcfyc9chM
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" plan-loop --bot larry-paper
start "" /b ".venv\Scripts\pythonw.exe" -m gate_bot --root "%CD%" paper-run --bot larry-paper
echo started larry-paper plan-loop + paper-run (no window)
