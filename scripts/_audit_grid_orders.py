#!/usr/bin/env python3
"""只读审计 martin-grid（ETH_USDT）：持仓 / 全部挂单 / 保护单，找出异常。"""
import json
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402

bot = load_bot_config(ROOT / "config/bots/martin-grid.yaml")
client = bot.create_client()
SYM = (bot.symbols or ["ETH_USDT"])[0]

# ── 持仓 ──
positions = client.get_positions()
mine = [p for p in positions if p.get("contract") == SYM]
print(f"=== {SYM} 持仓 ===")
for p in mine:
    print(json.dumps({k: p.get(k) for k in
                      ("contract", "mode", "size", "entry_price", "mark_price",
                       "unrealised_pnl", "leverage", "realised_pnl")}, ensure_ascii=False))

# ── 普通挂单（含 reduce_only 的网格 TP）──
print(f"\n=== {SYM} 普通挂单 list_orders ===")
orders = client.list_orders(SYM)
print(f"共 {len(orders)} 条")
for o in sorted(orders, key=lambda x: float(x.get("price") or 0)):
    print(f"  id={o.get('id')} price={o.get('price')} size={o.get('size')} "
          f"left={o.get('left')} reduce_only={o.get('reduce_only')} "
          f"finish_as={o.get('finish_as')} text={o.get('text')} create={o.get('create_time')}")

# ── 触发单（TP/SL price_orders）──
print(f"\n=== {SYM} 触发单 list_price_orders ===")
try:
    po = client.list_price_orders(SYM)
    print(f"共 {len(po)} 条")
    for o in po:
        trig = o.get("trigger") or {}
        initial = o.get("initial") or {}
        print(f"  id={o.get('id')} rule={o.get('rule')} trigger={trig.get('price')} "
              f"({trig.get('rule')}) size={initial.get('size')} close={initial.get('close')} "
              f"auto_size={initial.get('auto_size')} text={o.get('text')} "
              f"status={o.get('status')} expiry={o.get('expiry')}")
except Exception as e:  # noqa: BLE001
    print("price_orders 读取失败:", type(e).__name__, e)

# ── 汇总异常 ──
import time  # noqa: E402
mark = 0.0
for p in mine:
    mark = float(p.get("mark_price") or 0)
print(f"\n=== 汇总 === mark={mark}")
ro = [o for o in orders if o.get("reduce_only")]
print(f"普通挂单: 总 {len(orders)} | reduce_only(TP限价) {len(ro)} | 入场单 {len(orders)-len(ro)}")
for o in sorted(ro, key=lambda x: float(x.get("price") or 0)):
    px = float(o.get("price") or 0)
    left = float(o.get("left") or 0)
    if mark and px > mark * 1.02:
        print(f"  ⚠️ 远端TP: price={px} (mark×1.02={mark*1.02:.1f} 之上) left={left} text={o.get('text')}")
