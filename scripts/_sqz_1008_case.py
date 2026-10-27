#!/usr/bin/env python3
"""10-08 暴跌破案例解剖：信号时间线 + 反事实推演（闸门能省多少）。"""
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
width = [None] * n
mom = [None] * n
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
    width[i] = 4 * sd / m * 100
    mom[i] = closes[i] - closes[i - 1]

# 10-08 窗口：找 ts ∈ [10-08 00:00, 10-09 00:00] 的索引
t0 = int(time.mktime(time.strptime("2026-10-08 00:00:00", "%Y-%m-%d %H:%M:%S"))) - time.timezone
t1 = t0 + 86400
idx = [i for i in range(n) if t0 <= ts[i] < t1]
print(f"窗口 {len(idx)} 根：{time.strftime('%m-%d %H:%M', time.gmtime(ts[idx[0]]))} ~ "
      f"{time.strftime('%m-%d %H:%M', time.gmtime(ts[idx[-1]]+900))}\n")

# 每根 bar 的时间线
print(f"{'时间':>11s} {'close':>8s} {'state':>5s} {'带宽':>6s} {'动量Δ':>7s}  事件")
prev_state = state[idx[0] - 1] if idx[0] > 0 else None
sqz_run = 0
for i in idx:
    ev = ""
    if state[i] == 1:
        sqz_run += 1
        if prev_state != 1:
            ev = "← 挤压开始"
    elif state[i] == -1 and prev_state == 1:
        ev = f"← ★向下释放（挤压已持续 {sqz_run*0.25:.1f}h）"
    prev_state = state[i]
    t = time.strftime("%H:%M", time.gmtime(ts[i]))
    st = {1: "挤压", -1: "释放", 0: "无"}.get(state[i], "?")
    flag = "▊" if ev else ""
    print(f"{t:>11s} {closes[i]:>8.1f} {st:>5s} {width[i]:>5.2f}% {mom[i]:>+7.1f}  {ev}{flag}")
    if state[i] != 1:
        sqz_run = 0

# 反事实：以窗口首根价格为锚，按当前人格的阶梯形态布阵
anchor = closes[idx[0]]
levels = []
for pct, size, kind in [(-1.2, 1, "网格"), (-1.8, 1, "网格"), (-2.4, 1, "网格"),
                        (-2.8, 6, "马丁1"), (-3.9, 1, "网格"), (-4.6, 7, "马丁2"),
                        (-5.2, 1, "网格"), (-5.9, 1, "网格"), (-6.6, 9, "马丁3")]:
    levels.append({"price": anchor * (1 + pct / 100), "size": size, "kind": kind,
                   "pct": pct})
release_i = None
for i in idx:
    if state[i] == -1 and state[i - 1] == 1:
        release_i = i
        break
bottom = min(lows[i] for i in idx)
end_c = closes[idx[-1]]

print(f"\n=== 反事实推演（锚 {anchor:.1f}，梯形按现人格；最低 {bottom:.1f}）===")
print(f"释放时刻 = {time.strftime('%H:%M', time.gmtime(ts[release_i]))}"
      f"（闸门规则：此刻撤所有未成交低吸单，24h 不补）\n")
print(f"{'档位':>10s} {'价格':>8s} {'张数':>4s} {'触发时间':>9s} {'无闸门':>10s} {'有闸门':>10s}")
saved = {"G1": 0.0, "G2": 0.0}
for L in levels:
    fill_i = None
    for i in idx:
        if lows[i] <= L["price"]:
            fill_i = i
            break
    if fill_i is None:
        print(f"{L['kind']+L['pct'].__repr__() if False else L['kind']:>10s} "
              f"{L['price']:>8.1f} {L['size']:>4d} {'未触发':>9s}")
        continue
    ft = time.strftime("%H:%M", time.gmtime(ts[fill_i]))
    after_release = release_i is not None and fill_i > release_i
    mtm_bottom = (L["price"] - bottom) * 0.01 * L["size"]      # 到最低点的浮亏
    no_gate = f"成交@{ft}"
    if after_release:
        g1 = "马丁撤单 ✓" if L["kind"].startswith("马丁") else "成交"
        g2 = "全部撤单 ✓"
        if L["kind"].startswith("马丁"):
            saved["G1"] += mtm_bottom
        saved["G2"] += mtm_bottom
        print(f"{L['kind']:>10s} {L['price']:>8.1f} {L['size']:>4d} {ft:>9s} "
              f"{no_gate:>10s} {g1:>10s}  躲过浮亏 {mtm_bottom:.2f}U")
    else:
        print(f"{L['kind']:>10s} {L['price']:>8.1f} {L['size']:>4d} {ft:>9s} "
              f"{no_gate:>10s} {'成交（释放前）':>10s}")
print(f"\n释放后触发的浮亏合计：马丁层 {saved['G1']:.2f}U ｜ 马丁+网格全部 {saved['G2']:.2f}U")
print("（G1 = 只撤马丁；G2 = 撤全部低吸。浮亏按窗口最低点 2415 计，未含此后反弹回吐）")
