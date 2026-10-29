#!/usr/bin/env python3
"""近 12h order_history（SELECT *）与 trade_history 逐笔对照，找漏记的成交。"""
import sqlite3
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/account.db?mode=ro", uri=True)
lo = int(time.time()) - 12 * 3600

print("① order_history 表结构")
cols = [r[1] for r in con.execute("PRAGMA table_info(order_history)")]
print("   ", cols)

print("\n② 近 12h order_history（全部）")
q = "SELECT * FROM order_history WHERE timestamp > ? ORDER BY timestamp"
for r in con.execute(q, (lo,)):
    d = dict(zip(cols, r))
    print(f"   {time.strftime('%H:%M', time.gmtime(d['timestamp']))} "
          f"size={d.get('size')} left={d.get('left')} status={d.get('status')} "
          f"price={d.get('price')} text={d.get('text') or d.get('label')}")

print("\n③ 近 12h trade_history")
for r in con.execute(
        "SELECT timestamp, size, price, role, fee FROM trade_history "
        "WHERE contract='ETH_USDT' AND timestamp > ? ORDER BY timestamp", (lo,)):
    print(f"   {time.strftime('%H:%M', time.gmtime(r[0]))} size={r[1]} px={r[2]} "
          f"{r[3]} fee={r[4]}")
con.close()
