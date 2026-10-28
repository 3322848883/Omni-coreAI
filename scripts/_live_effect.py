#!/usr/bin/env python3
"""线上现状：动态档距是否生效 / 盘面 / 近 3h 成交与盈亏。"""
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))

print("=" * 62)
print("① 近 5 轮动态档距（线上仍是 atr_mult=1.0 的旧代码）")
print("=" * 62)
files = sorted(glob.glob(f"{ROOT}/data/bots/martin-grid/archive/done/*.result.json"),
               key=os.path.getmtime)[-5:]
for f in files:
    d = json.load(open(f, encoding="utf-8"))
    rep = d.get("report") or d
    ts = time.strftime("%H:%M", time.localtime(os.path.getmtime(f)))
    for s in (rep.get("steps") or []):
        if s.get("action") != "ladder_template":
            continue
        det = s.get("detail") or {}
        dyn = det.get("dynamic") or {}
        print(f"  {ts}  step={det.get('step_pct')}%  atr%={dyn.get('atr_pct')}  "
              f"bw%={dyn.get('bw_pct')}  raw={dyn.get('raw_step_pct')}  "
              f"placed={len(det.get('placed') or [])}")

print("\n" + "=" * 62)
print("② 当前盘面")
print("=" * 62)
from omnialpha.config import load_bot_config  # noqa: E402

c = load_bot_config(ROOT / "config/bots/martin-grid.yaml").create_client()
mark = c.get_last_price("ETH_USDT")
pos = 0.0
for p in c.get_positions():
    if p.get("contract") == "ETH_USDT" and float(p.get("size") or 0) != 0:
        pos = float(p.get("size"))
        print(f"  持仓 size={p.get('size')} entry={p.get('entry_price')} "
              f"uPnL={p.get('unrealised_pnl')}")
if pos == 0:
    print("  持仓 0")
print(f"  mark={mark}")
ent = [o for o in c.list_orders("ETH_USDT") if float(o.get("size") or 0) > 0]
print(f"  入场单 {len(ent)} 条，合计 {sum(float(o.get('size') or 0) for o in ent):g} 张")
for o in sorted(ent, key=lambda x: -float(x.get("price") or 0)):
    px = float(o.get("price") or 0)
    print(f"    {px:>9.2f}  ×{float(o.get('size') or 0):>3g}  "
          f"距 mark {(mark - px) / mark * 100:5.2f}%")
for o in c.list_price_orders("ETH_USDT"):
    i = o.get("initial") or {}
    print(f"  保护 trigger={(o.get('trigger') or {}).get('price')} size={i.get('size')} "
          f"rule={(o.get('trigger') or {}).get('rule')}")

print("\n" + "=" * 62)
print("③ 近 4h 成交")
print("=" * 62)
con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/account.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT timestamp, size, price, role, realised_pnl FROM trade_history "
    "WHERE contract='ETH_USDT' AND timestamp > ? ORDER BY timestamp DESC LIMIT 15",
    (int(time.time()) - 4 * 3600,)).fetchall()
if not rows:
    print("  (无成交)")
pnl = 0.0
for t, size, price, role, rp in rows:
    try:
        pnl += float(rp or 0)
    except (TypeError, ValueError):
        pass
    print(f"  {time.strftime('%H:%M', time.localtime(t))} size={size:>4} px={price} {role}")
print(f"  近 4h realised_pnl 合计 ≈ {pnl:.4f}")
con.close()
