#!/usr/bin/env python3
"""看 v2 之后连续几根新 bar 的填充情况 + 是否有 prime 日志。"""
import sqlite3
import subprocess
import time

DB = "/opt/omnialpha/pa-data-source/data/kline.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT t, ema20, ema50, ema200, atr14, rsi14, macd, boll20 FROM kline "
    "WHERE symbol='BTC_USDT' AND interval='1m' ORDER BY t DESC LIMIT 6").fetchall()
print("最近 6 根 1m（BTC）：")
for r in rows:
    filled = sum(1 for v in r[1:] if v is not None)
    print(f"  {time.strftime('%H:%M', time.gmtime(r[0]))} 填充={filled}/7  "
          f"ema20={r[1]}  boll20={r[7]}")
con.close()

print("\nprime/ingest 相关日志：")
print(subprocess.run(
    "journalctl -u omnialpha-kline-live --since '-15 min' --no-pager | "
    "grep -iE 'prime|seed|ingest engine' | tail -8", shell=True,
    capture_output=True, text=True).stdout.strip() or "(无)")
