#!/usr/bin/env python3
"""清理 7 条 size=0 垃圾挂单 + 复核。"""
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))
from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()
zero = [o for o in c.list_orders("ETH_USDT") if float(o.get("size") or 0) == 0]
print(f"清理前 size=0 挂单: {len(zero)} 条")
for o in zero:
    try:
        c.cancel_order(o["id"])
        print(f"  撤 {o['id']} price={o['price']}")
    except Exception as e:  # noqa: BLE001
        print(f"  撤 {o['id']} 失败: {type(e).__name__}: {str(e)[:60]}")
left = [o for o in c.list_orders("ETH_USDT") if float(o.get("size") or 0) == 0]
print(f"\n清理后 size=0: {len(left)} 条")
print("剩余挂单：")
for o in sorted(c.list_orders("ETH_USDT"), key=lambda x: float(x.get("price") or 0)):
    print(f"  price={o.get('price'):>8s} size={o.get('size'):>4} text={o.get('text')}")
print("\n触发单：")
for o in c.list_price_orders("ETH_USDT"):
    i = o.get("initial") or {}
    print(f"  trigger={(o.get('trigger') or {}).get('price')} size={i.get('size')}")
