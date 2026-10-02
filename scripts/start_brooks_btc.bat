@echo off
rem Brooks BTC 实盘机器人一键启动（plan-loop + run）
cd /d "%~dp0.."
set OPENAI_BASE_URL=https://api.deepseek.com/v1
if "%OPENAI_API_KEY%"=="" echo WARNING: OPENAI_API_KEY not set
if "%GATE_API_KEY%"=="" echo WARNING: GATE_API_KEY not set
start "brooks-btc-plan" /min ".venv\Scripts\python.exe" -m omnialpha plan-loop --bot brooks-btc
start "brooks-btc-run"  /min ".venv\Scripts\python.exe" -m omnialpha run --bot brooks-btc
echo started brooks-btc plan-loop + run
echo logs: logs\brooks-btc-*.log / *.err
