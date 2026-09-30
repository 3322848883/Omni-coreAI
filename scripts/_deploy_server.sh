#!/bin/bash
# 服务器部署脚本 v2 — gate-signal-bot v1.1.0
set -e
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo "=== 1) venv（已建则复用）==="
cd /opt/gate-signal-bot
if [ ! -d .venv ]; then
  uv venv --python 3.12 .venv
fi
.venv/bin/python --version

echo "=== 2) 依赖 ==="
uv pip install -q --python .venv/bin/python -r requirements.txt
uv pip install -q --python .venv/bin/python -e .
echo "INSTALL_OK"

echo "=== 3) 全量测试 ==="
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -6

echo "=== 4) status ==="
.venv/bin/python -m gate_bot status 2>&1 | head -6

echo "=== DEPLOY_SCRIPT_DONE ==="
