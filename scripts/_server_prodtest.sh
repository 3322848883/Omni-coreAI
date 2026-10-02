#!/bin/bash
# 服务器生产测试：用实盘密钥但 allow_actions 仅 hold（保证不下单）
set -e
cd /opt/omnialpha

echo "=== 配置 skill-real 为安全生产测试 ==="
python3 - <<'PY'
from pathlib import Path
p = Path("config/bots/skill-real.yaml")
t = p.read_text(encoding="utf-8")
t = t.replace("env: testnet", "env: live")
t = t.replace("api_key_env: GATE_TESTNET_API_KEY", "api_key_env: GATE_API_KEY")
t = t.replace("api_secret_env: GATE_TESTNET_API_SECRET", "api_secret_env: GATE_API_SECRET")
t = t.replace("allow_actions: [hold, open_long, open_short]", "allow_actions: [hold]")
t = t.replace("enabled: false", "enabled: true")
p.write_text(t, encoding="utf-8")
print("updated skill-real.yaml")
for line in t.splitlines():
    if any(k in line for k in ("env:", "allow_actions", "skills:", "enabled:", "symbols")):
        print("  ", line.strip())
PY

echo
echo "=== 运行一次 plan（真实 LLM + skill）==="
J=/opt/omnialpha/logs/skill_journal.jsonl
BEFORE=$(wc -l < "$J" 2>/dev/null || echo 0)
.venv/bin/python -m omnialpha plan --bot skill-real 2>&1 | tail -15
AFTER=$(wc -l < "$J" 2>/dev/null || echo 0)
echo
echo "journal: $BEFORE -> $AFTER"
echo "=== 新增 journal 记录 ==="
tail -5 "$J" 2>/dev/null
