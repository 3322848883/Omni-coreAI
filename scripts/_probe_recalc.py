#!/usr/bin/env python3
"""隔离测试：副本库上跑 recalc_series（单序列），确认 11 列是否写入。"""
import shutil
import sqlite3
import sys

sys.path.insert(0, "/opt/omnialpha/pa-data-source")
import recalc  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402

src = "/opt/omnialpha/pa-data-source/data/kline.db"
dst = "/tmp/kline_probe.db"
shutil.copyfile(src, dst)
con = sqlite3.connect(dst)
specs = parse_indicators(None)
print("specs:", [(s.kind, s.params, list(s.columns), s.warmup) for s in specs])

r = recalc.recalc_series(con, "SOL_USDT", "15m", specs, table="kline")
print("recalc 结果:", r)

cols = ["ema20", "ema50", "ema200", "atr14", "rsi14", "macd", "macd_dea",
        "macd_hist", "boll20", "boll20_upper", "boll20_lower"]
row = con.execute(
    f"SELECT t,{','.join(cols)} FROM kline WHERE symbol='SOL_USDT' AND interval='15m' "
    "ORDER BY t DESC LIMIT 1").fetchone()
print("\n最新行:", row)
print("\n各列非 NULL 行数：")
for i, c in enumerate(cols):
    n = con.execute(f"SELECT count({c}) FROM kline WHERE symbol='SOL_USDT' AND interval='15m'").fetchone()[0]
    tot = con.execute("SELECT count(*) FROM kline WHERE symbol='SOL_USDT' AND interval='15m'").fetchone()[0]
    print(f"  {c:14s} {n}/{tot}")
con.commit()
con.close()
