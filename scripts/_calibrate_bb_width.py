#!/usr/bin/env python3
"""标定 BB 带宽阈值：用 15m 真实数据算 (upper-lower)/middle 的分布。"""
import json
import statistics
import urllib.request

rows = json.loads(urllib.request.urlopen(
    "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    "?contract=ETH_USDT&interval=15m&limit=2000", timeout=30).read())
closes = [float(r["c"]) for r in rows]

widths = []
n = len(closes)
for i in range(19, n):
    w = closes[i - 19: i + 1]
    m = sum(w) / 20
    sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    if m > 0:
        widths.append(4 * sd / m * 100)  # (upper-lower)/middle = 4σ/m

widths.sort()
pct = lambda p: widths[min(len(widths) - 1, int(len(widths) * p))]
cur = widths[-1]
print(f"样本 {len(widths)} 根（约 {len(widths)*15/60/24:.0f} 天）")
print(f"当前带宽 = {cur:.2f}%")
for p in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95):
    print(f"  P{int(p*100):02d} = {pct(p):.2f}%")
