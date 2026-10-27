#!/usr/bin/env python3
"""区分「迁移写的历史行」与「重启后 watcher 写的新行」的指标填充情况。"""
import sqlite3
import time

con = sqlite3.connect("file:data/kline.db?mode=ro", uri=True)
now = int(time.time())

for label, lo, hi in (("历史（10-20 附近）", now - 8 * 86400, now - 7 * 86400),
                      ("迁移前 1h", now - 7200, now - 3600),
                      ("重启后最新", now - 900, now + 60)):
    row = con.execute(
        "SELECT t, ema20, ema50, ema200, atr14, rsi14, macd, boll20 "
        "FROM kline WHERE symbol='SOL_USDT' AND interval='15m' AND t BETWEEN ? AND ? "
        "ORDER BY t DESC LIMIT 1", (lo, hi)).fetchone()
    print(f"{label}: {row}")

print("\n按时间分段的 ema50 NULL 数（SOL 15m）：")
for lo, hi, name in ((now - 20 * 86400, now - 8 * 86400, "20d~8d 前"),
                     (now - 8 * 86400, now - 86400, "8d~1d 前"),
                     (now - 86400, now - 3600, "1d~1h 前"),
                     (now - 3600, now + 60, "近 1h（重启后）")):
    n = con.execute("SELECT count(*), sum(ema50 IS NULL) FROM kline "
                    "WHERE symbol='SOL_USDT' AND interval='15m' AND t BETWEEN ? AND ?",
                    (lo, hi)).fetchone()
    print(f"  {name:14s} 行数={n[0]:5d} ema50_NULL={n[1]}")
con.close()
