#!/usr/bin/env python3
"""SQZMOM 尾部风险检验：释放方向 × 挤压时长 → 未来 24h 深跌概率。"""
import json
import statistics
import time
import urllib.request

rows = json.loads(urllib.request.urlopen(
    "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    "?contract=ETH_USDT&interval=15m&limit=2000", timeout=30).read())
rows.sort(key=lambda r: int(r["t"]))
closes = [float(r["c"]) for r in rows]
highs = [float(r["h"]) for r in rows]
lows = [float(r["l"]) for r in rows]
ts = [int(r["t"]) for r in rows]
n = len(closes)

tr_l = [highs[0] - lows[0]]
for i in range(1, n):
    pc = closes[i - 1]
    tr_l.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))

state = [None] * n
for i in range(19, n):
    w = closes[i - 19: i + 1]
    m = sum(w) / 20
    sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    ubb, lbb = m + 2 * sd, m - 2 * sd
    kma = sum(closes[i - 19: i + 1]) / 20
    rma = sum(tr_l[i - 19: i + 1]) / 20
    ukc, lkc = kma + 1.5 * rma, kma - 1.5 * rma
    on = (lbb > lkc) and (ubb < ukc)
    off = (lbb < lkc) and (ubb > ukc)
    state[i] = 1 if on else (-1 if off else 0)

# 释放事件：state 1→-1，记录挤压持续时长 + 未来 24h 最深回撤/收盘收益
events = []
run = 0
for i in range(20, n):
    if state[i] == 1:
        run += 1
    elif state[i] == -1 and state[i - 1] == 1:
        if i + 96 < n:
            fwd_min = min((closes[i + k] - closes[i]) / closes[i] * 100
                          for k in range(1, 97))
            fwd_24h = (closes[i + 96] - closes[i]) / closes[i] * 100
            events.append({"t": ts[i], "dur_h": run * 0.25,
                           "mom_down": closes[i] < closes[i - 1],
                           "fwd_min": fwd_min, "fwd_24h": fwd_24h})
        run = 0
    elif state[i] == 0:
        run = 0

down = [e for e in events if e["mom_down"]]
up = [e for e in events if not e["mom_down"]]

print(f"释放事件 {len(events)} 次（21 天）\n")
print(f"向下释放 {len(down)} 次：")
print(f"  未来24h 最深回撤: 均值 {statistics.mean(e['fwd_min'] for e in down):+.2f}%"
      f"  最差 {min(e['fwd_min'] for e in down):+.2f}%")
tail_d = [e for e in down if e["fwd_min"] < -2]
print(f"  深跌(<-2%) : {len(tail_d)}/{len(down)}")
long_d = [e for e in down if e["dur_h"] >= 4]
tail_ld = [e for e in long_d if e["fwd_min"] < -2]
print(f"  挤压≥4h 的向下释放: {len(long_d)} 次 → 其中深跌 {len(tail_ld)} 次")
for e in long_d:
    print(f"    {time.strftime('%m-%d %H:%M', time.gmtime(e['t']))} "
          f"挤压{e['dur_h']:.1f}h → 24h最深 {e['fwd_min']:+.2f}% / 收盘 {e['fwd_24h']:+.2f}%")

print(f"\n向上释放 {len(up)} 次：")
print(f"  未来24h 最深回撤: 均值 {statistics.mean(e['fwd_min'] for e in up):+.2f}%"
      f"  最差 {min(e['fwd_min'] for e in up):+.2f}%")
tail_u = [e for e in up if e["fwd_min"] < -2]
print(f"  深跌(<-2%) : {len(tail_u)}/{len(up)}")
long_u = [e for e in up if e["dur_h"] >= 4]
tail_lu = [e for e in long_u if e["fwd_min"] < -2]
print(f"  挤压≥4h 的向上释放: {len(long_u)} 次 → 其中深跌 {len(tail_lu)} 次")

print(f"\n=== 闸门评估（规则：挤压≥4h 向下释放 → 关低吸 24h）===")
hit = len(tail_ld)
fp = len(long_d) - hit
print(f"捕获深跌 {hit} 次 / 误杀 {fp} 次 / 漏网（深跌但无此信号）"
      f" {len(tail_u)} 次 / 总深跌 {len(tail_d) + len(tail_u)} 次")
