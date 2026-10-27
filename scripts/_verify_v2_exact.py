#!/usr/bin/env python3
"""精确验证：排除 ema200 预热（前 199 根）后再数 NULL。"""
import sqlite3

con = sqlite3.connect("file:data/kline.db?mode=ro", uri=True)
print("tables:", [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
print("rows:", con.execute("SELECT count(*) FROM kline").fetchone()[0])
print("列数:", len([r[1] for r in con.execute("PRAGMA table_info(kline)")]))

print("\nmeta:", dict(con.execute("SELECT key, value FROM kline_meta").fetchall())
      if con.execute("SELECT count(*) FROM sqlite_master WHERE name='kline_meta'").fetchone()[0]
      else "(无)")

row = con.execute(
    "SELECT t, o, c, ema20, ema200, atr14, rsi14, boll20, boll20_upper "
    "FROM kline WHERE symbol='SOL_USDT' AND interval='15m' ORDER BY t DESC LIMIT 1").fetchone()
print("\n最新 SOL 15m:", row)

total_nulls = 0
bad_series = 0
for sym, iv in con.execute("SELECT DISTINCT symbol, interval FROM kline").fetchall():
    # 预热分界：该序列第 200 根（offset 199）的 t
    r = con.execute("SELECT t FROM kline WHERE symbol=? AND interval=? "
                    "ORDER BY t ASC LIMIT 1 OFFSET 199", (sym, iv)).fetchone()
    if not r:
        continue
    n = con.execute(
        "SELECT count(*) FROM kline WHERE symbol=? AND interval=? AND t>=? AND closed=1 "
        "AND (ema20 IS NULL OR ema200 IS NULL OR atr14 IS NULL OR boll20 IS NULL "
        "OR rsi14 IS NULL OR macd IS NULL OR macd_hist IS NULL)", (sym, iv, r[0])).fetchone()[0]
    total_nulls += n
    if n:
        bad_series += 1
        print(f"  预热后仍有 NULL: {sym} {iv}: {n}")
print(f"\n预热后 NULL 总数 = {total_nulls}（序列 {bad_series} 条有残留）")
con.close()
