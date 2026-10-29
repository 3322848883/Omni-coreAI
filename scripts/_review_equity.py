#!/usr/bin/env python3
"""权益变化 + 当前盘面（12h 复盘关键数字）。"""
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))
now = int(time.time())
since = now - 12 * 3600

con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/account.db?mode=ro", uri=True)
cols = [r[1] for r in con.execute("PRAGMA table_info(balance_history)")]
print("balance_history 列:", cols)


def bal_at(t):
    r = con.execute(
        "SELECT total, available, unrealised_pnl FROM balance_history "
        "WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1", (t,)).fetchone()
    return r


t0 = bal_at(since)
t1 = bal_at(now)
print(f"\n12h 前（{time.strftime('%m-%d %H:%M', time.gmtime(since))}）："
      f"总额 {t0[0] if t0 else '?'} 可用 {t0[1] if t0 else '?'} 未实现 {t0[2] if t0 else '?'}")
print(f"现在（{time.strftime('%m-%d %H:%M', time.gmtime(now))}）："
      f"总额 {t1[0] if t1 else '?'} 可用 {t1[1] if t1 else '?'} 未实现 {t1[2] if t1 else '?'}")
if t0 and t1:
    a, b = float(t0[0]), float(t1[0])
    print(f"→ 权益变化 {b - a:+.4f} U（{100 * (b - a) / a:+.2f}%）")

print("\n近 12h 权益曲线（每 60 分钟采样）")
for h in range(12, -1, -1):
    t = now - h * 3600
    r = bal_at(t)
    if r:
        print(f"  {time.strftime('%H:%M', time.gmtime(t))}  {float(r[0]):.4f}")
con.close()

from omnialpha.config import load_bot_config  # noqa: E402

c = load_bot_config(ROOT / "config/bots/martin-grid.yaml").create_client()
mark = c.get_last_price("ETH_USDT")
pos = [(p.get("side"), float(p.get("size") or 0), p.get("entry_price"))
       for p in c.get_positions() if p.get("contract") == "ETH_USDT"]
print(f"\n当前 mark={mark}  持仓={pos}")
ent = [o for o in c.list_orders("ETH_USDT") if float(o.get("size") or 0) > 0]
print(f"入场挂单 {len(ent)} 条")
for o in sorted(ent, key=lambda x: -float(x.get("price") or 0)):
    print(f"   {float(o.get('price') or 0):>9.2f} ×{float(o.get('size') or 0):>3g} {o.get('text')}")
for o in c.list_price_orders("ETH_USDT"):
    print(f"   保护 trigger={(o.get('trigger') or {}).get('price')} "
          f"size={(o.get('initial') or {}).get('size')}")
