#!/usr/bin/env python3
"""核实报告：交易所真相 + 最近计划 chips + 0 张单来源。"""
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))
from omnialpha.config import load_bot_config  # noqa: E402

BOT = "/opt/omnialpha/data/bots/martin-grid"
bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
c = bot.create_client()

print("=" * 60)
print("① 交易所当前状态（真相）")
print("=" * 60)
for p in c.get_positions():
    if p.get("contract") == "ETH_USDT" and float(p.get("size") or 0) != 0:
        print(f"持仓 {p.get('mode')} size={p.get('size')} entry={p.get('entry_price')}")

print("\n限价挂单：")
for o in sorted(c.list_orders("ETH_USDT"), key=lambda x: float(x.get("price") or 0)):
    sz = float(o.get("size") or 0)
    flag = "  ← size=0 无效单" if sz == 0 else ""
    print(f"  price={o.get('price'):>8s} size={o.get('size'):>4} left={o.get('left'):>4} "
          f"text={o.get('text')}{flag}")

print("\n触发单：")
for o in c.list_price_orders("ETH_USDT"):
    i = o.get("initial") or {}
    print(f"  trigger={(o.get('trigger') or {}).get('price'):>8s} size={i.get('size'):>5} "
          f"text={o.get('text')}")

print("\n" + "=" * 60)
print("② 最近 4 份计划文件的 chips（谁挂的 0 张单 / SL -25）")
print("=" * 60)
files = sorted(glob.glob(f"{BOT}/archive/*/*.json") + glob.glob(f"{BOT}/state/*.hold.json"),
               key=os.path.getmtime)[-8:]
for f in files[-4:]:
    if f.endswith(".thinking.json") or f.endswith(".error.json"):
        continue
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    orders = d.get("orders") or d.get("chips") or []
    print(f"\n--- {os.path.basename(f)}  ({len(orders)} 条) ---")
    for o in orders[:12]:
        if not isinstance(o, dict):
            continue
        print(f"  {o.get('action'):<14} price={o.get('price')} size={o.get('size')} "
              f"size_usd={o.get('size_usd')} sl={o.get('sl')}")

print("\n" + "=" * 60)
print("③ 引擎：size=0 是否被放行？（executor 关键片段）")
print("=" * 60)
import re  # noqa: E402

src = open(ROOT / "omnialpha/executor.py", encoding="utf-8").read()
for kw in ("q <= 0", "size_min", "SIZE_INVALID"):
    for m in list(re.finditer(kw, src))[:2]:
        s = max(0, m.start() - 90)
        print(f"  [{kw}] …{src[s:m.start()+120].replace(chr(10), ' ')}…")
