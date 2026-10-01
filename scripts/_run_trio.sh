#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /C:/Users/w6485/Desktop/测试/gate-signal-bot
export GATE_BOT_ROOT="C:/Users/w6485/Desktop/测试/gate-signal-bot"
# 读取本地密钥
if [ -f scripts/secrets.bat ]; then
  export OPENAI_BASE_URL=$(grep -m1 'OPENAI_BASE_URL=' scripts/secrets.bat | sed 's/.*=//' | tr -d '\r')
  export OPENAI_API_KEY=$(grep -m1 'OPENAI_API_KEY=' scripts/secrets.bat | sed 's/.*=//' | tr -d '\r')
fi
echo "=== 三阶段讨论单次试跑 ==="
echo "成员: pa-a(价格行为) / smc-paper(SMC) / orderflow-paper(订单流)"
echo "融合: weighted_vote + 讨论 3 轮"
echo ""
.venv/Scripts/python.exe -m gate_bot --root "$GATE_BOT_ROOT" persona-run --group disc-trio --once 2>&1
echo ""
echo "=== 讨论日志 ==="
if [ -f data/shared/persona_log.jsonl ]; then
  tail -8 data/shared/persona_log.jsonl
fi
echo "=== TRIO_DONE ==="
