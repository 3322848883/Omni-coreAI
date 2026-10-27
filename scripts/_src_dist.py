#!/usr/bin/env python3
"""查 src 分布 + 表清单：确认现在的 kline 是迁移结果还是 watcher 覆盖过。"""
import sqlite3

con = sqlite3.connect("file:data/kline.db?mode=ro", uri=True)
print("表:", [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")])
print("\n按 src 统计（SOL 15m）:")
for src, n, filled in con.execute(
        "SELECT src, count(*), sum(ema50 IS NOT NULL) FROM kline "
        "WHERE symbol='SOL_USDT' AND interval='15m' GROUP BY src"):
    print(f"  src={src!r} rows={n} ema50非NULL={filled}")

print("\n历史行（10-20）:")
row = con.execute("SELECT t, src, ema20, ema50, boll20 FROM kline WHERE symbol='SOL_USDT' "
                  "AND interval='15m' AND t < 1792600000 ORDER BY t DESC LIMIT 1").fetchone()
print(" ", row)

print("\nuser_version:", con.execute("PRAGMA user_version").fetchone()[0])
print("meta:", dict(con.execute("SELECT key,value FROM kline_meta").fetchall())
      if con.execute("SELECT count(*) FROM sqlite_master WHERE name='kline_meta'").fetchone()[0] else "(无)")
con.close()
