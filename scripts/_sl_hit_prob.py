#!/usr/bin/env python3
"""止损位被扫概率：用 ETH 15m 最近 20 天算「未来 N 根内最大回撤」分布。

回答：SL 距 mark −5.79%（新版）vs −7.33%（旧版），哪个更容易被插针扫掉。
"""
import sqlite3
from pathlib import Path

DB = Path("/opt/omnialpha/pa-data-source/data/kline.db")
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT t, c, l FROM kline WHERE symbol='ETH_USDT' AND interval='15m' "
    "AND closed=1 ORDER BY t DESC LIMIT 2000").fetchall()
con.close()
rows.reverse()
closes = [r[1] for r in rows]
lows = [r[2] for r in rows]
print(f"样本 {len(rows)} 根 15m ≈ {len(rows) * 15 / 60 / 24:.1f} 天")

HORIZONS = (4, 8, 16, 32)      # 1h / 2h / 4h / 8h
for h in HORIZONS:
    maes = []
    for i in range(len(rows) - h):
        c0 = closes[i]
        if not c0:
            continue
        worst = min(lows[i + 1:i + 1 + h])
        maes.append((c0 - worst) / c0 * 100)
    maes.sort()
    n = len(maes)

    def pct(q):
        return maes[int(n * q) - 1]

    print(f"\n未来 {h} 根（{h * 15} 分钟）内最大回撤：")
    print(f"  P50 {pct(0.5):5.2f}%   P75 {pct(0.75):5.2f}%   P90 {pct(0.9):5.2f}%   "
          f"P99 {pct(0.99):5.2f}%   max {maes[-1]:5.2f}%")
    for label, thr in (("新版 SL 5.79%", 5.79), ("旧版 SL 7.33%", 7.33),
                       ("若放宽到 6.4%", 6.4), ("若放宽到 7.0%", 7.0)):
        hit = sum(1 for m in maes if m >= thr) / n * 100
        print(f"    触及 {label:14s} 的概率 {hit:5.2f}%")
