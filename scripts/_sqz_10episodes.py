#!/usr/bin/env python3
"""10 段不同行情检验：42 天历史（游标分页）+ zigzag 3%，取 5 跌 + 5 涨最大段。"""
import json
import time
import urllib.request

# ── to 游标分页拉 4000 根 15m（约 42 天）──
all_rows: dict[int, dict] = {}
cursor = None
for _ in range(6):
    qs = "contract=ETH_USDT&interval=15m&limit=1000" + (f"&to={cursor}" if cursor else "")
    rows = json.loads(urllib.request.urlopen(
        f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?{qs}", timeout=30).read())
    if not rows:
        break
    for r in rows:
        all_rows[int(r["t"])] = r
    earliest = min(int(r["t"]) for r in rows)
    if cursor is not None and earliest >= cursor:
        break
    cursor = earliest - 1
    time.sleep(0.3)
rows = [all_rows[t] for t in sorted(all_rows)]
rows = rows[-4000:]
n = len(rows)
closes = [float(r["c"]) for r in rows]
highs = [float(r["h"]) for r in rows]
lows = [float(r["l"]) for r in rows]
ts = [int(r["t"]) for r in rows]
print(f"历史 {n} 根 15m：{time.strftime('%m-%d', time.gmtime(ts[0]))} ~ "
      f"{time.strftime('%m-%d', time.gmtime(ts[-1]))}（{n*15/60/24:.0f} 天）")

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

# ── zigzag 3% ──
TH = 3.0
pivots = []
trend = 0
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
            pivots.append((run_min_i, run_min))
            trend, ext_i, ext_p = 1, i, p
        elif p <= run_max * (1 - TH / 100):
            pivots.append((run_max_i, run_max))
            trend, ext_i, ext_p = -1, i, p
    elif trend == 1:
        if p > ext_p:
            ext_i, ext_p = i, p
        elif p <= ext_p * (1 - TH / 100):
            pivots.append((ext_i, ext_p))
            trend, ext_i, ext_p = -1, i, p
    else:
        if p < ext_p:
            ext_i, ext_p = i, p
        elif p >= ext_p * (1 + TH / 100):
            pivots.append((ext_i, ext_p))
            trend, ext_i, ext_p = 1, i, p
pivots.append((ext_i, ext_p))

LADDER = [(-1.2, 1, "网格"), (-1.8, 1, "网格"), (-2.4, 1, "网格"), (-2.8, 6, "马丁1"),
          (-3.9, 1, "网格"), (-4.6, 7, "马丁2"), (-5.2, 1, "网格"), (-5.9, 1, "网格"),
          (-6.6, 9, "马丁3")]


def fmt(t):
    return time.strftime("%m-%d", time.gmtime(t))


def squeeze_lead(i_from, i_to, want_dir):
    """首个「挤压≥4h → want_dir 释放」；返回 (dur_h, rel_i) 或 None。"""
    run = 0
    for i in range(max(19, i_from), i_to + 1):
        if state[i] == 1:
            run += 1
        elif state[i] == -1:
            if run >= 16:
                d = "down" if closes[i] < closes[i - 1] else "up"
                if d == want_dir:
                    return run * 0.25, i
            run = 0
        else:
            run = 0
    return None


legs = []
for k in range(1, len(pivots)):
    (i0, p0), (i1, p1) = pivots[k - 1], pivots[k]
    if i1 > i0:
        legs.append({"i0": i0, "i1": i1, "p0": p0, "p1": p1,
                     "chg": (p1 - p0) / p0 * 100})
down_legs = sorted([l for l in legs if l["chg"] <= -TH], key=lambda l: l["chg"])[:5]
up_legs = sorted([l for l in legs if l["chg"] >= TH], key=lambda l: -l["chg"])[:5]

print("\n" + "=" * 76)
print("5 段最大下跌：挤压前兆 + G1 反事实")
print("=" * 76)
hits = 0
g1_all = 0.0
for leg in down_legs:
    bottom = min(lows[leg["i0"]: leg["i1"] + 1])
    lead = squeeze_lead(leg["i0"], leg["i1"], "down")
    move = f"{leg['p0']:.0f}→{leg['p1']:.0f} ({leg['chg']:+.1f}%)"
    span = f"{fmt(ts[leg['i0']])}→{fmt(ts[leg['i1']])}"
    if lead:
        dur, rel_i = lead
        lead_h = (ts[leg["i1"]] - ts[rel_i]) / 3600
        saved = 0.0
        parts = []
        for pct, size, kind in LADDER:
            price = leg["p0"] * (1 + pct / 100)
            fill_i = next((k for k in range(leg["i0"], leg["i1"] + 1)
                           if lows[k] <= price), None)
            if fill_i is None or fill_i <= rel_i or not kind.startswith("马丁"):
                continue
            mtm = (price - bottom) * 0.01 * size
            saved += mtm
            parts.append(f"{kind} {price:.0f} 躲 {mtm:.2f}U")
        hits += 1
        g1_all += saved
        print(f"✓ {span}  {move}\n    挤压{dur:.1f}h→down释放@{fmt(ts[rel_i])}"
              f"（领先{lead_h:.1f}h）G1省 {saved:.2f}U  [{'；'.join(parts) if parts else '无马丁触发'}]")
    else:
        print(f"✗ {span}  {move}\n    无挤压≥4h 前兆")
print(f"\n下跌段命中 {hits}/5，G1 合计省 {g1_all:.2f}U")

print("\n" + "=" * 76)
print("5 段最大上涨：向上释放前兆 + 向下释放误触发")
print("=" * 76)
up_hit = 0
fp = 0
for leg in up_legs:
    hi = max(highs[leg["i0"]: leg["i1"] + 1])
    move = f"{leg['p0']:.0f}→{leg['p1']:.0f} (+{leg['chg']:+.1f}%)"
    span = f"{fmt(ts[leg['i0']])}→{fmt(ts[leg['i1']])}"
    lead_up = squeeze_lead(leg["i0"], leg["i1"], "up")
    lead_dn = squeeze_lead(leg["i0"], leg["i1"], "down")
    tags = []
    if lead_up:
        dur, rel_i = lead_up
        up_hit += 1
        tags.append(f"✓ 挤压{dur:.1f}h→up释放@{fmt(ts[rel_i])}")
    if lead_dn:
        dur, rel_i = lead_dn
        fp += 1
        tags.append(f"⚠️ down释放@{fmt(ts[rel_i])}（误杀补格）")
    if not tags:
        tags.append("○ 无挤压信号")
    print(f"{span}  {move}\n    " + "；".join(tags))
print(f"\n上涨段：向上释放前兆 {up_hit}/5，向下释放误触发 {fp}/5")
