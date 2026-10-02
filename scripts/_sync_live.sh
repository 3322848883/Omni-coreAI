#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/omnialpha

echo "=== 1) 合并密钥到 .env ==="
if [ -f /tmp/gate_env ]; then
  # 替换空的 GATE_API_KEY / GATE_API_SECRET
  KEY=$(grep '^GATE_API_KEY=' /tmp/gate_env | cut -d= -f2)
  SECRET=$(grep '^GATE_API_SECRET=' /tmp/gate_env | cut -d= -f2)
  sed -i "s|^GATE_API_KEY=.*|GATE_API_KEY=$KEY|" .env
  sed -i "s|^GATE_API_SECRET=.*|GATE_API_SECRET=$SECRET|" .env
  rm -f /tmp/gate_env
  echo "  OK 密钥已写入 .env"
else
  echo "  ERROR /tmp/gate_env 不存在"
  exit 1
fi

echo "=== 2) 验证 .env（脱敏）==="
sed 's/=.*/=***/' .env

echo "=== 3) 测试 Gate 连通 + 账户 ==="
.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, ".")
from pathlib import Path
# 加载 .env
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from omnialpha.config import load_bot_config
cfg = load_bot_config(Path("config/bots/brooks-btc.yaml"))
client = cfg.create_client()
acct = client.get_account() or {}
print("  账户 total:", acct.get("total"))
print("  available :", acct.get("available"))
pos = [p for p in (client.get_positions() or []) if int(p.get("size") or 0) != 0]
print("  持仓数    :", len(pos))
for p in pos:
    print("   ", p.get("contract"), p.get("size"), p.get("entry_price"))
orders = client.list_price_orders("BTC_USDT") or []
open_orders = [o for o in orders if str(o.get("status","")).lower() in ("open","triggered","untriggered","new")]
print("  挂单数    :", len(open_orders))
print("  GATE_OK")
PY

echo "=== 4) 重启看门狗加载密钥 ==="
systemctl restart omnialpha-watchdog
sleep 6
systemctl is-active omnialpha-watchdog

echo "=== SYNC_DONE ==="
