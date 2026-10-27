#!/usr/bin/env python3
"""彻底修复第三步：撤 6 条旧 gtp 限价 TP + 3 条口径不符的触发单（用户已授权）。"""
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()
SYM = "ETH_USDT"

GTP_IDS = ["63894859558222233", "63894859635509067", "63894859486627211",
           "63894859500055035", "63894859593203689", "63894859471594340"]
PRICE_IDS = ["2115100973377523712", "2115100963676098560", "2115089030654726144"]

print("--- 撤 6 条旧 gtp 限价 TP ---")
for oid in GTP_IDS:
    try:
        r = c.cancel_order(oid)
        print(f"  {oid}: {json.dumps(r, ensure_ascii=False)[:80]}")
    except Exception as e:  # noqa: BLE001
        print(f"  {oid}: FAIL {type(e).__name__}: {str(e)[:90]}")

print("--- 撤 3 条触发单（SL34 / TP9 / TP1）---")
for pid in PRICE_IDS:
    try:
        r = c.cancel_price_order(pid)
        print(f"  {pid}: {json.dumps(r, ensure_ascii=False)[:80]}")
    except Exception as e:  # noqa: BLE001
        print(f"  {pid}: FAIL {type(e).__name__}: {str(e)[:90]}")

print("--- 复核：剩余挂单 ---")
orders = c.list_orders(SYM)
po = c.list_price_orders(SYM)
print(f"限价 {len(orders)} 条 | 触发 {len(po)} 条")
for o in orders:
    print(f"  {o.get('text')} price={o.get('price')} size={o.get('size')}")
for o in po:
    print(f"  {o.get('text')} trigger={(o.get('trigger') or {}).get('price')} "
          f"size={(o.get('initial') or {}).get('size')}")
