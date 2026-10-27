#!/usr/bin/env python3
"""回填所有序列的 11 列指标（修 legacy 写入器把指标列清空的问题）+ 验证。"""
import sqlite3
import sys
import time

sys.path.insert(0, "/opt/omnialpha/pa-data-source")
import recalc  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402

DB = "/opt/omnialpha/pa-data-source/data/kline.db"
con = sqlite3.connect(DB, timeout=60)
con.execute("PRAGMA busy_timeout=60000")
specs = parse_indicators(None)

print("=== 回填前：最新 bar ===")
row = con.execute("SELECT t, src, ema20, ema50, ema200, atr14, rsi14, macd, boll20 "
                  "FROM kline WHERE symbol='SOL_USDT' AND interval='15m' "
                  "ORDER BY t DESC LIMIT 1").fetchone()
print(" ", row)

series = con.execute("SELECT DISTINCT symbol, interval FROM kline").fetchall()
print(f"\n=== 回填 {len(series)} 个序列 ===")
t0 = time.time()
total = 0
bad = []
for sym, iv in series:
    r = recalc.recalc_series(con, sym, iv, specs, table="kline")
    total += r["updated"]
    if r.get("missing"):
        bad.append((sym, iv, r["missing"]))
print(f"更新 {total} 行，耗时 {time.time()-t0:.0f}s，缺失列序列 {len(bad)}")

print("\n=== 回填后：最新 bar ===")
row = con.execute("SELECT t, src, ema20, ema50, ema200, atr14, rsi14, macd, boll20 "
                  "FROM kline WHERE symbol='SOL_USDT' AND interval='15m' "
                  "ORDER BY t DESC LIMIT 1").fetchone()
print(" ", row)

print("\n=== 预热后 NULL 复核（11 列全查）===")
cols = ["ema20", "ema50", "ema200", "atr14", "rsi14", "macd", "macd_dea",
        "macd_hist", "boll20", "boll20_upper", "boll20_lower"]
tot_null = 0
worst = []
for sym, iv in series:
    rr = con.execute("SELECT t FROM kline WHERE symbol=? AND interval=? "
                     "ORDER BY t ASC LIMIT 1 OFFSET 199", (sym, iv)).fetchone()
    if not rr:
        continue
    expr = " + ".join(f"([{c}] IS NULL)" for c in cols)
    n = con.execute(f"SELECT sum({expr}) FROM kline WHERE symbol=? AND interval=? "
                    f"AND t>=? AND closed=1", (sym, iv, rr[0])).fetchone()[0] or 0
    tot_null += n
    if n:
        worst.append((sym, iv, n))
print(f"预热后 NULL 合计 = {tot_null}")
for w in worst[:5]:
    print("  仍 NULL:", w)
con.close()
