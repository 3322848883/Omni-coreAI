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

cfg = load_bot_config(Path("config/bots/brooks-btc.yaml"))
c = cfg.create_client()
acct = c.get_account() or {}
print("账户 total:", acct.get("total"))
pos = [p for p in (c.get_positions() or []) if int(p.get("size") or 0) != 0]
print("持仓:", len(pos))
for p in pos:
    print("  ", p.get("contract"), p.get("size"), p.get("entry_price"))
print("--- 全部挂单 ---")
for o in (c.list_price_orders("BTC_USDT") or []):
    init = o.get("initial") or {}
    trig = o.get("trigger") or {}
    ro = init.get("reduce_only") or o.get("reduce_only")
    print(f"  id={str(o.get('id'))[-8:]} text={init.get('text') or o.get('text')} "
          f"size={init.get('size') or o.get('size')} trigger={trig.get('price')} "
          f"status={o.get('status')} reduce_only={ro}")

print("--- 用 executor 做孤儿检测 ---")
from omnialpha.executor import Executor
ex = Executor(c, label_prefix="brk", root=Path("."), bot_id="brooks-btc")
rows = c.list_price_orders("BTC_USDT") or []
positions = c.get_positions() or []
orphans = []
for p in rows:
    init = p.get("initial") or {}
    text = str(init.get("text") or p.get("text") or "")
    tail = text.rsplit("-", 1)[-1].lower() if text else ""
    st = str(p.get("status") or "").lower()
    ro = init.get("reduce_only") or p.get("reduce_only")
    is_or = ex._is_orphan_protector(p, positions)
    print(f"  {text:<12} tail={tail:<4} ro={ro} status={st:<12} orphan={is_or}")
    if is_or:
        orphans.append(p)
print("孤儿数量:", len(orphans))
PY
echo "=== ORPHAN_CHECK_DONE ==="
