#!/usr/bin/env python3
"""现状体检：为方案排序提供数据（锚点漂移成本 / 模型主动开平 / PnL 落库）。"""
import glob
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")

print("=" * 64)
print("① 锚点漂移成本（近 10 轮 placed 数量 + anchor 变化）")
print("=" * 64)
files = sorted(glob.glob(f"{ROOT}/data/bots/martin-grid/archive/done/*.result.json"),
               key=os.path.getmtime)[-10:]
tot = 0
for f in files:
    d = json.load(open(f, encoding="utf-8"))
    rep = d.get("report") or d
    ts = time.strftime("%H:%M", time.localtime(os.path.getmtime(f)))
    for s in (rep.get("steps") or []):
        if s.get("action") != "ladder_template":
            continue
        det = s.get("detail") or {}
        n = len(det.get("placed") or [])
        tot += n
        print(f"  {ts} anchor={det.get('anchor')} step={det.get('step_pct')}% "
              f"placed={n} pruned={len(det.get('pruned_off_template') or [])}")
print(f"  近 10 轮共挂 {tot} 次（每轮 ≈ 撤同数）→ 日化 ≈ {tot / 10 * 24 * 4:.0f} 次 API 写")

print("\n" + "=" * 64)
print("② 模型主动开平（近 6h，taker 占比）")
print("=" * 64)
con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/account.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT timestamp, size, price, role, realised_pnl FROM trade_history "
    "WHERE contract='ETH_USDT' AND timestamp > ? ORDER BY timestamp DESC",
    (int(time.time()) - 6 * 3600,)).fetchall()
taker = [r for r in rows if str(r[3]) == "taker"]
pnl_zero = sum(1 for r in rows if str(r[4] or "0") in ("0", "0.0", ""))
print(f"  成交 {len(rows)} 笔，其中 taker {len(taker)} 笔"
      f"（{len(taker) / max(len(rows), 1) * 100:.0f}%）")
print(f"  realised_pnl 为 0 的记录：{pnl_zero}/{len(rows)}")
for r in rows[:6]:
    print(f"    {time.strftime('%H:%M', time.localtime(r[0]))} size={r[1]:>4} "
          f"px={r[2]} {r[3]} pnl={r[4]}")
con.close()

print("\n" + "=" * 64)
print("③ 模板 vs 模型：谁在管保护")
print("=" * 64)
sys.path.insert(0, str(ROOT))
from omnialpha.config import load_bot_config  # noqa: E402
c = load_bot_config(ROOT / "config/bots/martin-grid.yaml").create_client()
pos = 0.0
for p in c.get_positions():
    if p.get("contract") == "ETH_USDT":
        pos += abs(float(p.get("size") or 0))
ent = sum(float(o.get("size") or 0) for o in c.list_orders("ETH_USDT")
          if float(o.get("size") or 0) > 0)
print(f"  持仓 {pos:g} + 入场 {ent:g} = 应覆盖 {pos + ent:g}")
for o in c.list_price_orders("ETH_USDT"):
    i = o.get("initial") or {}
    print(f"  保护 trigger={(o.get('trigger') or {}).get('price')} "
          f"size={i.get('size')} rule={(o.get('trigger') or {}).get('rule')} "
          f"text={i.get('text')}")
