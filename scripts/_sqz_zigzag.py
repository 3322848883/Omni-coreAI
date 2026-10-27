#!/usr/bin/env python3
"""多空行情段检验 v3：zigzag 3% 摆动点切分，逐段验证挤压信号与闸门价值。"""
import json
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

# ── zigzag 3% 转向 ──
TH = 3.0
pivots = []
trend = 0          # 1=向上腿 -1=向下腿 0=未定
ext_i, ext_p = 0, closes[0]
run_max_i, run_max = 0, closes[0]
run_min_i, run_min = 0, closes[0]
for i in range(1, n):
    p = closes[i]
    if trend == 0:
        if p > run_max:
            run_max_i, run_max = i, p
        if p < run_min:
            run_min_i, run_min = i, p
        if p >= run_min * (1 + TH / 100):
            pivots.append((run_min_i, run_min))      # 起点低点
            trend, ext_i, ext_p = 1, i, p
        elif p <= run_max * (1 - TH / 100):
            pivots.append((run_max_i, run_max))      # 起点高点
            trend, ext_i, ext_p = -1, i, p
    elif trend == 1:
        if p > ext_p:
            ext_i, ext_p = i, p
        elif p <= ext_p * (1 - TH / 100):
            pivots.append((ext_i, ext_p))            # 顶
            trend, ext_i, ext_p = -1, i, p
    else:
        if p < ext_p:
            ext_i, ext_p = i, p
        elif p >= ext_p * (1 + TH / 100):
            pivots.append((ext_i, ext_p))            # 底
            trend, ext_i, ext_p = 1, i, p
pivots.append((ext_i, ext_p))

LADDER = [(-1.2, 1, "网格"), (-1.8, 1, "网格"), (-2.4, 1, "网格"), (-2.8, 6, "马丁1"),
          (-3.9, 1, "网格"), (-4.6, 7, "马丁2"), (-5.2, 1, "网格"), (-5.9, 1, "网格"),
          (-6.6, 9, "马丁3")]


def fmt(t):
    return time.strftime("%m-%d %H:%M", time.gmtime(t))


def squeeze_lead(i_from, i_to, want_dir):
    run = 0
    for i in range(max(19, i_from), i_to + 1):
        if state[i] == 1:
            run += 1
        elif state[i] == -1:
            if run >= 16:
                d = "down" if closes[i] < closes[i - 1] else "up"
                if d == want_dir:
                    return {"dur": run * 0.25, "rel_i": i}
            run = 0
        else:
            run = 0
    return None


n_down = n_up = 0
g1_list = []
down_no_signal = 0
up_false = up_ok = 0
legs = [(pivots[k - 1], pivots[k]) for k in range(1, len(pivots))]
for (i0, p0), (i1, p1) in legs:
    if i1 <= i0:
        continue
    chg = (p1 - p0) / p0 * 100
    t0, t1 = fmt(ts[i0]), fmt(ts[i1])
    if chg <= -TH:
        n_down += 1
        bottom = min(lows[i0: i1 + 1])
        lead = squeeze_lead(i0, i1, "down")
        saved = 0.0
        detail = []
        if lead:
            for pct, size, kind in LADDER:
                price = p0 * (1 + pct / 100)
                fill_i = next((k for k in range(i0, i1 + 1) if lows[k] <= price), None)
                if fill_i is None or fill_i <= lead["rel_i"]:
                    continue
                mtm = (price - bottom) * 0.01 * size
                if kind.startswith("马丁"):
                    saved += mtm
                detail.append(f"{kind} {price:.0f} 躲 {mtm:.2f}U")
        lead_h = (ts[i1] - ts[lead["rel_i"]]) / 3600 if lead else None
        if lead and saved > 0:
            g1_list.append(saved)
            print(f"▼ {t0} → {t1}  {p0:.0f}→{p1:.0f} ({chg:+.1f}%)  "
                  f"✓ 挤压{lead['dur']:.1f}h→down释放@{fmt(ts[lead['rel_i']])}"
                  f"（领先{lead_h:.1f}h）G1省 {saved:.2f}U  [{'；'.join(detail[:3])}]")
        elif lead:
            print(f"▼ {t0} → {t1}  {p0:.0f}→{p1:.0f} ({chg:+.1f}%)  "
                  f"△ 挤压{lead['dur']:.1f}h→down释放，但无马丁触发（省 0）")
        else:
            down_no_signal += 1
            print(f"▼ {t0} → {t1}  {p0:.0f}→{p1:.0f} ({chg:+.1f}%)  ✗ 无挤压前兆")
    elif chg >= TH:
        n_up += 1
        lead = squeeze_lead(i0, i1, "down")
        if lead:
            up_false += 1
            lead_h = (ts[i1] - ts[lead["rel_i"]]) / 3600
            print(f"▲ {t0} → {t1}  {p0:.0f}→{p1:.0f} ({chg:+.1f}%)  "
                  f"⚠️ 误触发：挤压{lead['dur']:.1f}h→down释放@{fmt(ts[lead['rel_i']])}"
                  f"（领先{lead_h:.1f}h）")
        else:
            up_ok += 1
            print(f"▲ {t0} → {t1}  {p0:.0f}→{p1:.0f} ({chg:+.1f}%)  ✓ 无误触发")

print(f"\n=== 汇总（zigzag 3%，21 天，{n_down} 跌段 + {n_up} 涨段）===")
print(f"下跌段: 有前兆且省到马丁的 {len(g1_list)} 段（G1 合计 {sum(g1_list):.2f}U）｜"
      f"无前兆 {down_no_signal} 段")
print(f"上涨段: 误触发 {up_false} 段（错杀补格）｜无误触发 {up_ok} 段")
