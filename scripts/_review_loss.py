#!/usr/bin/env python3
"""定位 02:00 前后 -4.69U 的来源：订单历史 / 持仓变化 / 资金费。"""
import sqlite3
import time
from pathlib import Path

ROOT = Path("/opt/omnialpha")
con = sqlite3.connect(f"file:{ROOT}/pa-data-source/data/account.db?mode=ro", uri=True)
lo = int(time.time()) - 12 * 3600
hi = int(time.time())

print("① order_history 02:00±90min（fill 记录）")
a, b = 1793248800, 1793255400        # 01:20 ~ 03:10 UTC
for r in con.execute(
        "SELECT * FROM order_history WHERE contract='ETH_USDT' "
        "AND timestamp BETWEEN ? AND ? ORDER BY timestamp", (a, b)):
    print("   ", time.strftime('%H:%M', time.gmtime(r[1])), r[2:])

print("\n② position_history 01:20~03:10（size 变化）")
prev = None
for r in con.execute(
        "SELECT timestamp, mode, size, entry_price, mark_price, unrealised_pnl "
        "FROM position_history WHERE contract='ETH_USDT' AND timestamp BETWEEN ? AND ? "
        "ORDER BY timestamp", (a, b)):
    if r[2] != prev:
        print(f"   {time.strftime('%H:%M', time.gmtime(r[0]))} {r[1]} size={r[2]} "
              f"entry={r[3]} mark={r[4]} upnl={r[5]}")
        prev = r[2]

print("\n③ 权益每 10 分钟（01:00~03:00）")
for t in range(a - 1200, b + 600, 600):
    r = con.execute("SELECT total FROM balance_history WHERE timestamp <= ? "
                    "ORDER BY timestamp DESC LIMIT 1", (t,)).fetchone()
    if r:
        print(f"   {time.strftime('%H:%M', time.gmtime(t))} {float(r[0]):.4f}")

print("\n④ 近 12h trade_history 全量（含未记录 realised_pnl 的）")
n = con.execute("SELECT count(*), sum(abs(size)) FROM trade_history "
                "WHERE contract='ETH_USDT' AND timestamp > ?", (lo,)).fetchone()
print(f"   {n[0]} 笔，总张数 {n[1]}")
print("\n⑤ 是否有可能漏记录的成交（order_history 与 trade_history 数量对比）")
o = con.execute("SELECT count(*) FROM order_history WHERE contract='ETH_USDT' "
                "AND timestamp > ? AND status='finished'", (lo,)).fetchone()[0]
print(f"   order_history finished={o}  trade_history={n[0]}")
con.close()
