#!/usr/bin/env python3
"""定位：prime 修复是否还在服务器 + NULL 从哪个小时开始。"""
import sqlite3
import subprocess
import time

print("=== ① 服务器 ingest.py 是否有 prime 修复 ===")
print(subprocess.run("grep -c '首次写入前预热' /opt/omnialpha/pa-data-source/ingest.py",
                     shell=True, capture_output=True, text=True).stdout.strip())
print(subprocess.run("grep -n 'prime(conn' /opt/omnialpha/pa-data-source/ingest.py",
                     shell=True, capture_output=True, text=True).stdout.strip() or "(无 prime 调用)")

print("\n=== ② 服务重启历史 ===")
print(subprocess.run(
    "systemctl show -p NRestarts --value omnialpha-kline-live; "
    "systemctl show -p ActiveEnterTimestamp --value omnialpha-kline-live; "
    "journalctl -u omnialpha-kline-live --since '-10 hour' --no-pager | "
    "grep -i 'Started\\|primed' | tail -6", shell=True, capture_output=True,
    text=True).stdout.strip())

print("\n=== ③ 按小时统计 NULL 率（BTC 15m，最近 12h）===")
con = sqlite3.connect("file:/opt/omnialpha/pa-data-source/data/kline.db?mode=ro", uri=True)
now = int(time.time())
for h in range(11, -1, -1):
    lo, hi = now - (h + 1) * 3600, now - h * 3600
    r = con.execute(
        "SELECT count(*), sum(ema50 IS NULL) FROM kline WHERE symbol='BTC_USDT' "
        "AND interval='15m' AND t>=? AND t<? AND closed=1", (lo, hi)).fetchone()
    if r[0]:
        print(f"  {time.strftime('%H:%M', time.gmtime(lo))}-{time.strftime('%H:%M', time.gmtime(hi))}: "
              f"{r[1]}/{r[0]} NULL")
con.close()
