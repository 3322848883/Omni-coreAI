#!/usr/bin/env python3
"""对账：交易所真实触发单 vs bot 快照视角（account.db price_order_current）。"""
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()

print("=== 交易所真实触发单 ===")
po = c.list_price_orders("ETH_USDT")
for o in po:
    i = o.get("initial") or {}
    print(f"  id={o.get('id')} trigger={(o.get('trigger') or {}).get('price')} "
          f"size={i.get('size')} reduce_only={i.get('is_reduce_only')} create={o.get('create_time')}")

print("\n=== account.db price_order_current（bot 快照来源）===")
db = sqlite3.connect("file:/opt/omnialpha/pa-data-source/data/account.db?mode=ro", uri=True)
for row in db.execute(
        "SELECT price_order_id, trigger_price, initial_size, status, update_time "
        "FROM price_order_current WHERE contract='ETH_USDT'"):
    print(" ", row)
print("\nnow =", int(time.time()))
db.close()
