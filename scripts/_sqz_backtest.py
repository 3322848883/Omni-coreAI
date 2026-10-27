#!/usr/bin/env python3
"""SQZMOM 有效性检验：历史释放事件的前瞻收益 + 与带宽规则的一致性。"""
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


def sma(v, p, i):
    if i + 1 < p:
        return None
    return sum(v[i - p + 1: i + 1]) / p


def stdev_(v, p, i):
    if i + 1 < p:
        return None
    w = v[i - p + 1: i + 1]
    m = sum(w) / p
    return (sum((x - m) ** 2 for x in w) / p) ** 0.5


# sqzmom 状态（BB 2.0 / KC 1.5 TR，与实现一致）
state = [None] * n
width = [None] * n
tr_l = [None] * n
for i in range(n):
    if i == 0:
        tr_l[i] = highs[i] - lows[i]
    else:
        pc = closes[i - 1]
        tr_l[i] = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
for i in range(19, n):
    m = sma(closes, 20, i)
    sd = stdev_(closes, 20, i)
    ubb, lbb = m + 2 * sd, m - 2 * sd
    kma = sma(closes, 20, i)
    rma = sma(tr_l, 20, i)
    ukc, lkc = kma + 1.5 * rma, kma - 1.5 * rma
    on = (lbb > lkc) and (ubb < ukc)
    off = (lbb < lkc) and (ubb > ukc)
    state[i] = 1 if on else (-1 if off else 0)
    width[i] = 4 * sd / m * 100

# 释放事件：state 1→-1 转换
events = []
for i in range(20, n):
    if state[i] == -1 and state[i - 1] == 1:
        mom_up = closes[i] > closes[i - 1]  # 释放首根方向（简化动量方向）
        fwd = {}
        for h in (12, 48, 96):  # 3h / 12h / 24h
            if i + h < n:
                fwd[f"{h//4}h"] = (closes[i + h] - closes[i]) / closes[i] * 100
        events.append({"t": ts[i], "dir": "up" if mom_up else "down",
                       "fwd": fwd, "width_at": width[i]})

print(f"样本 {n} 根（{n*15/60/24:.0f} 天）| 释放事件 {len(events)} 次\n")
for d in ("up", "down"):
    evs = [e for e in events if e["dir"] == d]
    if not evs:
        continue
    print(f"=== 向{'上' if d=='up' else '下'}释放 {len(evs)} 次 ===")
    for h in ("3h", "12h", "24h"):
        vals = [e["fwd"][h] for e in evs if h in e["fwd"]]
        neg = sum(1 for v in vals if v < 0)
        print(f"  未来{h:>3s}: 均值 {statistics.mean(vals):+.2f}%  中位 {statistics.median(vals):+.2f}%"
              f"  下跌占比 {neg}/{len(vals)}")
    print()

# 10-08 暴跌核查
print("=== 10-08 暴跌（2535→2417）核查 ===")
for i in range(20, n):
    if ts[i] > 1792230000 and closes[i] and min(closes[i:i + 40]) < 2430 and ts[i] < 1792320000:
        print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(ts[i]))} close={closes[i]:.1f} "
              f"state={state[i]} width={width[i]:.2f}%")
# 找 dump 前最后一次 state 变化
prev_s = None
for i in range(20, n):
    if not (1792150000 < ts[i] < 1792350000):
        continue
    if state[i] != prev_s:
        print(f"  状态变化 {time.strftime('%m-%d %H:%M', time.gmtime(ts[i]))}: "
              f"{prev_s} → {state[i]}  close={closes[i]:.1f} width={width[i]:.2f}%")
        prev_s = state[i]

# 与带宽规则一致性：挤压中带宽 <0.7% 的占比
sqz_bars = [width[i] for i in range(20, n) if state[i] == 1 and width[i] is not None]
sqz_w_lt07 = sum(1 for w in sqz_bars if w < 0.7)
print(f"\n=== 口径对比 ===")
print(f"挤压 bar 共 {len(sqz_bars)} 根，其中带宽 <0.7% 的 {sqz_w_lt07} 根"
      f"（{sqz_w_lt07/max(1,len(sqz_bars))*100:.0f}% 重合）")
print(f"带宽 <0.7% 的 bar 全场 {sum(1 for w in width[19:] if w is not None and w < 0.7)} 根")
