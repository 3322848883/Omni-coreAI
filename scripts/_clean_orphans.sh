#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/omnialpha
.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, ".")
from pathlib import Path
for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from omnialpha.config import load_bot_config
from omnialpha.executor import Executor

cfg = load_bot_config(Path("config/bots/brooks-btc.yaml"))
client = cfg.create_client()
ex = Executor(client, label_prefix="brk", root=Path("."), bot_id="brooks-btc")

print("--- 清理前 ---")
orders = client.list_price_orders("BTC_USDT") or []
for o in orders:
    init = o.get("initial") or {}
    print(f"  {init.get('text') or o.get('text')} size={init.get('size') or o.get('size')} id={str(o.get('id'))[-8:]}")
print("挂单数:", len(orders))

print("--- 执行孤儿清理 ---")
cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
print("已撤:", cleaned)

print("--- 清理后 ---")
orders2 = client.list_price_orders("BTC_USDT") or []
for o in orders2:
    init = o.get("initial") or {}
    print(f"  {init.get('text') or o.get('text')} size={init.get('size') or o.get('size')}")
print("挂单数:", len(orders2))

acct = client.get_account() or {}
print("账户 total:", acct.get("total"))
PY
echo "=== CLEAN_DONE ==="
