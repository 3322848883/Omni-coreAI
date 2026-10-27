#!/usr/bin/env python3
"""等新 bar（1m）后验证：11 列保持 + shadow 对拍日志。"""
import sqlite3
import subprocess
import time

DB = "/opt/omnialpha/pa-data-source/data/kline.db"
now_max = None
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
now_max = con.execute("SELECT max(t) FROM kline WHERE symbol='SOL_USDT' AND interval='1m'").fetchone()[0]
con.close()
print("当前最新 1m t =", now_max, time.strftime("%H:%M:%S", time.gmtime(now_max)))

for _ in range(24):          # 最多等 4 分钟
    time.sleep(10)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute(
        "SELECT t, src, ema20, ema50, ema200, atr14, rsi14, macd, boll20, boll20_upper "
        "FROM kline WHERE symbol='SOL_USDT' AND interval='1m' ORDER BY t DESC LIMIT 1").fetchone()
    con.close()
    if row and row[0] > now_max:
        print("\n=== 新 bar 已写入 ===")
        print(" t=", row[0], time.strftime("%H:%M:%S", time.gmtime(row[0])), "| src=", row[1])
        cols = ["ema20", "ema50", "ema200", "atr14", "rsi14", "macd", "boll20", "boll20_upper"]
        for c, v in zip(cols, row[2:]):
            print(f"   {c:12s} {v}")
        filled = sum(1 for v in row[2:] if v is not None)
        print(f"\n 指标填充 {filled}/{len(cols)}")
        break
else:
    print("4 分钟内没有新 bar（可能行情无成交或写入异常）")

print("\n=== shadow 对拍日志（最近 6 行）===")
out = subprocess.run(
    "journalctl -u omnialpha-kline-live --since '-10 min' --no-pager | grep -i shadow | tail -6",
    shell=True, capture_output=True, text=True).stdout.strip()
print(out or "（暂无 shadow 日志行）")

print("\n=== 服务状态 ===")
print(subprocess.run("systemctl is-active omnialpha-kline-live", shell=True,
                     capture_output=True, text=True).stdout.strip())
