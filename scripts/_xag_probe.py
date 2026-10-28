#!/usr/bin/env python3
"""查 XAG_USDT 4h 为什么指标全 NULL。"""
import sqlite3
import sys

sys.path.insert(0, "/opt/omnialpha/pa-data-source")
import recalc  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402

con = sqlite3.connect("file:/opt/omnialpha/pa-data-source/data/kline.db?mode=ro", uri=True)
print("XAG 4h 行数:", con.execute(
    "SELECT count(*), sum(closed=1), sum(o IS NULL), sum(c IS NULL) FROM kline "
    "WHERE symbol='XAG_USDT' AND interval='4h'").fetchone())
print("\n前 3 行:")
for r in con.execute("SELECT t,o,h,l,c,ema20,ema50,atr14,boll20,closed FROM kline "
                     "WHERE symbol='XAG_USDT' AND interval='4h' ORDER BY t LIMIT 3"):
    print(" ", r)
print("\n后 3 行:")
for r in con.execute("SELECT t,o,h,l,c,ema20,ema50,atr14,boll20,closed FROM kline "
                     "WHERE symbol='XAG_USDT' AND interval='4h' ORDER BY t DESC LIMIT 3"):
    print(" ", r)
print("\nNULL 分布（按行位置区间）:")
rows = con.execute("SELECT t, ema20 IS NULL FROM kline WHERE symbol='XAG_USDT' "
                   "AND interval='4h' ORDER BY t").fetchall()
import itertools
cnt = 0
for is_null, grp in itertools.groupby(rows, key=lambda x: x[1]):
    g = list(grp)
    print(f"  {'NULL' if is_null else '有值'}: {len(g)} 行")
con.close()

# 试着重算一次看结果
con = sqlite3.connect("/opt/omnialpha/pa-data-source/data/kline.db", timeout=30)
r = recalc.recalc_series(con, "XAG_USDT", "4h", parse_indicators(None), table="kline")
print("\n重算结果:", r)
row = con.execute("SELECT ema20, ema50, boll20 FROM kline WHERE symbol='XAG_USDT' "
                  "AND interval='4h' ORDER BY t DESC LIMIT 1").fetchone()
print("重算后最新行:", row)
con.commit()
con.close()
