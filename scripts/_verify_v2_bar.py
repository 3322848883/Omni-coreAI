#!/usr/bin/env python3
"""v2 启动回补后再回填 + 验证新 bar 是否写满 11 列。"""
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, "/opt/omnialpha/pa-data-source")
import recalc  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402

DB = "/opt/omnialpha/pa-data-source/data/kline.db"
print("等启动回补完成（90s）…")
time.sleep(90)

con = sqlite3.connect(DB, timeout=60)
con.execute("PRAGMA busy_timeout=60000")
specs = parse_indicators(None)
n = 0
for sym, iv in con.execute("SELECT DISTINCT symbol, interval FROM kline").fetchall():
    n += recalc.recalc_series(con, sym, iv, specs, table="kline")["updated"]
print(f"回填 {n} 行")

def snap(tag):
    r = con.execute(
        "SELECT t, src, ema20, ema50, ema200, atr14, rsi14, macd, boll20 "
        "FROM kline WHERE symbol='BTC_USDT' AND interval='1m' ORDER BY t DESC LIMIT 1").fetchone()
    filled = sum(1 for v in r[2:] if v is not None)
    print(f"{tag}: t={time.strftime('%H:%M:%S', time.gmtime(r[0]))} src={r[1]} "
          f"填充={filled}/{len(r)-2}")
    return r[0]

t0 = snap("回填后最新")
print("\n等新 bar 写入（最多 3 分钟）…")
for _ in range(18):
    time.sleep(10)
    r = con.execute("SELECT max(t) FROM kline WHERE symbol='BTC_USDT' AND interval='1m'").fetchone()[0]
    if r > t0:
        snap("新 bar")
        break
else:
    print("3 分钟内无新 bar")
con.close()

print("\n服务:", subprocess.run("systemctl is-active omnialpha-kline-live", shell=True,
                               capture_output=True, text=True).stdout.strip())
print("错误日志:", subprocess.run(
    "journalctl -u omnialpha-kline-live --since '-5 min' --no-pager | "
    "grep -icE 'error|traceback|fallback'", shell=True, capture_output=True,
    text=True).stdout.strip())
