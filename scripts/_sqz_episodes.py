#!/usr/bin/env python3
"""多空行情段检验 v2：段检测去重 12h + 挤压扫描覆盖全窗口（极值之前）。"""
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

WIN = 20          # 5h 窗口
GAP = 48          # 12h 去重
LADDER = [(-1.2, 1, "网格"), (-1.8, 1, "网格"), (-2.4, 1, "网格"), (-2.8, 6, "马丁1"),
          (-3.9, 1, "网格"), (-4.6, 7, "马丁2"), (-5.2, 1, "网格"), (-5.9, 1, "网格"),
          (-6.6, 9, "马丁3")]


def fmt(t):
    return time.strftime("%m-%d %H:%M", time.gmtime(t))


# ── 候选段：20 根窗口内 高→低（跌）或 低→高（涨）──
cands = []
for i in range(WIN, n):
    hi_w = max(highs[i - WIN: i + 1])
    lo_w = min(lows[i - WIN: i + 1])
    drop = (hi_w - lo_w) / hi_w * 100
    rally = (hi_w - lo_w) / lo_w * 100
    cands.append({"i": i, "drop": drop, "rally": rally})
cands.sort(key=lambda c: -c["drop"])
down_eps = []
for c in cands:
    if c["drop"] < 3.0:
        break
    if all(abs(c["i"] - e["i"]) > GAP for e in down_eps):
        down_eps.append(c)
cands.sort(key=lambda c: -c["rally"])
up_eps = []
for c in cands:
    if c["rally"] < 3.0:
        break
    if all(abs(c["i"] - e["i"]) > GAP for e in up_eps):
        up_eps.append(c)


def analyze_down(i_end):
    hi_i = max(range(i_end - WIN, i_end + 1), key=lambda k: highs[k])
    lo_i = min(range(hi_i, i_end + 1), key=lambda k: lows[k])
    anchor = closes[hi_i]
    bottom = lows[lo_i]
    lead = None
    run = 0
    for i in range(max(19, i_end - 96), i_end + 1):
        if state[i] == 1:
            run += 1
        elif state[i] == -1:
            if run >= 16:
                lead = {"dur": run * 0.25, "rel_i": i,
                        "dir": "down" if closes[i] < closes[i - 1] else "up",
                        "rel_price": closes[i]}
                break
            run = 0
        else:
            run = 0
    saved_g1 = saved_g2 = 0.0
    detail = []
    if lead:
        for pct, size, kind in LADDER:
            price = anchor * (1 + pct / 100)
            fill_i = next((k for k in range(hi_i, i_end + 1) if lows[k] <= price), None)
            if fill_i is None or fill_i <= lead["rel_i"]:
                continue
            mtm = (price - bottom) * 0.01 * size
            if kind.startswith("马丁"):
                saved_g1 += mtm
            saved_g2 += mtm
            detail.append(f"{kind} {price:.0f}×{size} 躲过 {mtm:.2f}U")
    return {"t0": ts[hi_i], "t1": ts[i_end], "anchor": anchor, "bottom": bottom,
            "drop": drop, "lead": lead, "saved_g1": saved_g1,
            "saved_g2": saved_g2, "detail": detail}


def analyze_up(i_end):
    lo_i = min(range(i_end - WIN, i_end + 1), key=lambda k: lows[k])
    hi = max(highs[lo_i: i_end + 1])
    fp = None
    run = 0
    for i in range(max(19, i_end - 96), i_end + 1):
        if state[i] == 1:
            run += 1
        elif state[i] == -1:
            if run >= 16 and closes[i] < closes[i - 1]:
                fp = {"t": ts[i], "dur": run * 0.25}
                break
            run = 0
        else:
            run = 0
    return {"t0": ts[lo_i], "t1": ts[i_end], "hi": hi, "rally": None, "fp": fp}


print("=" * 74)
print("下跌段（网格死因场景）：挤压前兆 + G1（撤马丁 24h）反事实")
print("=" * 74)
for c in down_eps:
    r = analyze_down(c["i"])
    ld = r["lead"]
    move = f"{r['anchor']:.0f}→{r['bottom']:.0f} ({-r['drop']:+.1f}%)"
    print(f"\n{fmt(r['t0'])} → {fmt(r['t1'])}  {move}")
    if ld:
        lead_h = (r["t1"] - ts[ld["rel_i"]]) / 3600
        print(f"  前兆: 挤压{ld['dur']:.1f}h → {ld['dir']}释放 @ {fmt(ts[ld['rel_i']])}"
              f"（价格 {ld['rel_price']:.0f}，领先段尾 {lead_h:.1f}h）")
        if ld["dir"] == "down" and r["saved_g1"] > 0:
            print(f"  闸门: ✓ G1 省 {r['saved_g1']:.2f}U（{'；'.join(r['detail'])}）")
        elif ld["dir"] == "down":
            print("  闸门: △ 有信号但段内无马丁触发（省 0）")
        else:
            print("  闸门: ✗ 释放方向向上，规则不触发")
    else:
        print("  前兆: ✗ 段内无 ≥4h 挤压→释放")

print()
print("=" * 74)
print("上涨段（网格盈利场景）：检查闸门是否误触发（向下释放=误杀补格）")
print("=" * 74)
for c in up_eps:
    r = analyze_up(c["i"])
    move = f"{r['hi']:.0f} (+{c['rally']:+.1f}%)"
    if r["fp"]:
        print(f"  {fmt(r['t0'])} → {fmt(r['t1'])}  {move}  "
              f"⚠️ 误触发: 向下释放@{fmt(r['fp']['t'])}（挤压{r['fp']['dur']:.1f}h）→ 会错杀补格")
    else:
        print(f"  {fmt(r['t0'])} → {fmt(r['t1'])}  {move}  ✓ 无误触发")
