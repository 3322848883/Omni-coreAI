#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/omnialpha
.venv/bin/python - <<'PY'
import os, sys, json
sys.path.insert(0, ".")
from pathlib import Path
for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from omnialpha.config import load_bot_config
cfg = load_bot_config(Path("config/bots/brooks-btc.yaml"))
c = cfg.create_client()
orders = c.list_price_orders("BTC_USDT") or []
print("订单数:", len(orders))
if orders:
    print("--- 第一张原始字段 ---")
    o = orders[0]
    print(json.dumps(o, ensure_ascii=False, indent=2, default=str)[:1500])
    print("--- 所有订单的 reduce 相关字段 ---")
    for o in orders:
        init = o.get("initial") or {}
        text = init.get("text") or o.get("text")
        keys = [k for k in list(o.keys()) + list(init.keys()) if "reduce" in k.lower() or "ro" == k.lower()]
        vals = {k: (init.get(k) if k in init else o.get(k)) for k in keys}
        print(f"  {text}: {vals}")
PY
echo "=== RAW_DONE ==="
