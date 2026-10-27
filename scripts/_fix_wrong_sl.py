#!/usr/bin/env python3
"""撤掉 reduce_only=False 的错误 SL，复核最终保护矩阵。"""
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()

BAD = "2115113729422004224"   # reduce_only=False 的错误 SL
try:
    r = c.cancel_price_order(BAD)
    print(f"撤 {BAD}: {json.dumps(r, ensure_ascii=False)[:100]}")
except Exception as e:  # noqa: BLE001
    print(f"撤 {BAD}: FAIL {type(e).__name__}: {str(e)[:100]}")

po = c.list_price_orders("ETH_USDT")
print(f"\n最终触发单 {len(po)} 条：")
ok = True
for o in po:
    i = o.get("initial") or {}
    ro = i.get("is_reduce_only") or i.get("reduce_only")
    line = (f"  id={o.get('id')} trigger={(o.get('trigger') or {}).get('price')} "
            f"size={i.get('size')} reduce_only={ro} text={o.get('text')}")
    if not ro:
        ok = False
        line += "  ← 仍有非 reduce_only！"
    print(line)
print("CHECK", "OK" if ok else "FAIL")
