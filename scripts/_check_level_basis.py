#!/usr/bin/env python3
"""核实：档位是否有结构依据 + 止损价的距离依据（ATR / 最深档）。"""
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

f = sorted(glob.glob(f"{ROOT}/data/bots/martin-grid/archive/done/*.result.json"),
           key=os.path.getmtime)[-1]
d = json.load(open(f, encoding="utf-8"))
rep = d.get("report") or d
det = {}
for s in (rep.get("steps") or []):
    if s.get("action") == "ladder_template" and (s.get("detail") or {}).get("step_pct"):
        det = s["detail"]
prices = sorted((float(o["price"]) for o in (det.get("placed") or [])), reverse=True)
anchor = det.get("anchor")
step = det.get("step_pct")
print(f"=== 最近一轮（{os.path.basename(f)[:15]}）")
print(f"  anchor={anchor}  step={step}%  层数={len(prices)}")
print(f"  档位: {[round(p, 2) for p in prices]}")

con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/kline.db?mode=ro", uri=True)
atr, mid, up, lo = con.execute(
    "SELECT atr14, boll20, boll20_upper, boll20_lower FROM kline "
    "WHERE symbol='ETH_USDT' AND interval='15m' AND closed=1 "
    "ORDER BY t DESC LIMIT 1").fetchone()
con.close()
print(f"\n  15m ATR14={atr:.2f}  BB中轨={mid:.2f} 上={up:.2f} 下={lo:.2f}")

print("\n=== 止损依据核对 ===")
c = load_cfg = None
from omnialpha.config import load_bot_config  # noqa: E402
cli = load_bot_config(ROOT / "config/bots/martin-grid.yaml").create_client()
mark = cli.get_last_price("ETH_USDT")
pos = 0.0
for p in cli.get_positions():
    if p.get("contract") == "ETH_USDT":
        pos += abs(float(p.get("size") or 0))
print(f"  mark={mark}  持仓={pos:g}")
deepest = min(prices) if prices else None
for o in cli.list_price_orders("ETH_USDT"):
    trg = (o.get("trigger") or {})
    ini = (o.get("initial") or {})
    if int(trg.get("rule") or 0) == 2:
        sl = float(trg.get("price") or 0)
        print(f"  SL={sl}  size={ini.get('size')}")
        if deepest:
            gap = deepest - sl
            print(f"    → 距最深档 {gap:.2f} 点 = {gap / atr:.2f} × ATR"
                  f"（人格要求 ≥1×ATR）")
        print(f"    → 距 mark {100 * (mark - sl) / mark:.2f}%")
        # 结构位：近 200 根 15m 的最低价（粗略 swing low 参考）
        con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/kline.db?mode=ro", uri=True)
        lo200 = con.execute(
            "SELECT min(l) FROM (SELECT l FROM kline WHERE symbol='ETH_USDT' "
            "AND interval='15m' AND closed=1 ORDER BY t DESC LIMIT 200)").fetchone()[0]
        con.close()
        print(f"    → 近 200 根 15m 最低 {lo200:.2f}（SL 是否在其下方："
              f"{'是' if sl < lo200 else '否'}）")
