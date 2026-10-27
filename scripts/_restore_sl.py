#!/usr/bin/env python3
"""恢复丢失的 SL：2165 / -6 张 / reduce_only（与 bot 自己此前的计划一致）。"""
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()

body = {
    "initial": {
        "contract": "ETH_USDT",
        "size": -6,
        "price": "0",
        "tif": "ioc",
        "text": "t-mtg-sl",
        "reduce_only": True,
    },
    "trigger": {
        "strategy_type": 0,
        "price_type": 1,          # mark 价，与 bot 原口径同源
        "price": "2165",
        "rule": 2,                # 2 = ≤ 触发
        "expiration": 0,
    },
}
r = c.place_price_order(body)
print(json.dumps({"id": r.get("id"), "status": r.get("status"),
                  "trigger": (r.get("trigger") or {}).get("price"),
                  "size": (r.get("initial") or {}).get("size")}, ensure_ascii=False))

po = c.list_price_orders("ETH_USDT")
print(f"当前触发单 {len(po)} 条：")
for o in po:
    i = o.get("initial") or {}
    print(f"  trigger={(o.get('trigger') or {}).get('price')} size={i.get('size')} "
          f"reduce_only={i.get('is_reduce_only')} text={o.get('text')}")
