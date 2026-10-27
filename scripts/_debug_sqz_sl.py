#!/usr/bin/env python3
"""排查：① sqzmom 工具调用的实际返回 ② 当前 SL 的真实 size。"""
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

# ① thinking 里 sqzmom 调用结果
f = max(glob.glob("/opt/omnialpha/data/bots/martin-grid/state/*thinking.json"),
        key=os.path.getmtime)
d = json.load(open(f, encoding="utf-8"))
print("=== sqzmom 工具调用记录 ===")
for t in d.get("tool_usage") or []:
    if isinstance(t, dict) and "sqzmom" in str(t.get("name") or t.get("tool")):
        print(json.dumps(t, ensure_ascii=False)[:600])

# ② 交易所真实触发单
from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()
print("\n=== 交易所触发单 ===")
for o in c.list_price_orders("ETH_USDT"):
    i = o.get("initial") or {}
    print(f"  trigger={(o.get('trigger') or {}).get('price')} size={i.get('size')} "
          f"reduce_only={i.get('is_reduce_only') or i.get('reduce_only')}")

print("\n=== 持仓 ===")
for p in c.get_positions():
    if p.get("contract") == "ETH_USDT" and float(p.get("size") or 0) != 0:
        print(f"  {p.get('mode')} size={p.get('size')} entry={p.get('entry_price')}")

print("\n=== 当前限价挂单数 ===")
orders = c.list_orders("ETH_USDT")
print(f"  {len(orders)} 条: {sorted((o.get('price'), o.get('size')) for o in orders)}")
