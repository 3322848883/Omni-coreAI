#!/usr/bin/env python3
"""逐列统计预热后 NULL，定位是哪一列没写。"""
import sqlite3

con = sqlite3.connect("file:data/kline.db?mode=ro", uri=True)
cols = [c for c in (r[1] for r in con.execute("PRAGMA table_info(kline)"))
        if c not in ("t", "o", "h", "l", "c", "v", "sum", "symbol", "interval", "closed")]
r = con.execute("SELECT t FROM kline WHERE symbol='SOL_USDT' AND interval='15m' "
                "ORDER BY t ASC LIMIT 1 OFFSET 199").fetchone()
print("列:", cols)
print(f"\nSOL 15m 预热后（t>={r[0]}）逐列 NULL 数：")
for c in cols:
    n = con.execute(f"SELECT count(*) FROM kline WHERE symbol='SOL_USDT' AND interval='15m' "
                    f"AND t>=? AND [{c}] IS NULL", (r[0],)).fetchone()[0]
    print(f"  {c:14s} NULL={n}")
# 看一行实际值
row = con.execute(f"SELECT {','.join(cols)} FROM kline WHERE symbol='SOL_USDT' "
                  f"AND interval='15m' ORDER BY t DESC LIMIT 1").fetchone()
print("\n最新行各列值：")
for c, v in zip(cols, row):
    print(f"  {c:14s} {v}")
con.close()
