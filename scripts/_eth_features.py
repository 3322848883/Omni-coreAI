"""提取 ETH 价格行为分析所需的结构特征。"""
import json
from pathlib import Path

SKILL = Path("skills/price-action-trading")
DATA = SKILL / "data"


def ema(vals, n):
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [None] * len(bars)
    if len(trs) >= n:
        prev = sum(trs[:n]) / n
        out[n] = prev
        for i in range(n, len(trs)):
            prev = (prev * (n - 1) + trs[i]) / n
            out[i + 1] = prev
    return out


def pivots(bars, lb=3):
    ph, pl = [], []
    for i in range(lb, len(bars) - lb):
        w = bars[i - lb:i + lb + 1]
        if bars[i]["h"] == max(x["h"] for x in w) and bars[i]["h"] > max(
            x["h"] for j, x in enumerate(w) if j != lb
        ):
            ph.append((i, bars[i]["h"]))
        if bars[i]["l"] == min(x["l"] for x in w) and bars[i]["l"] < min(
            x["l"] for j, x in enumerate(w) if j != lb
        ):
            pl.append((i, bars[i]["l"]))
    return ph, pl


for tf in ("4h", "1h", "5m"):
    bars = json.loads((DATA / f"eth_usdt_{tf}.json").read_text(encoding="utf-8"))
    for b in bars:
        for k in ("o", "h", "l", "c"):
            b[k] = float(b[k])
    closes = [b["c"] for b in bars]
    e20 = ema(closes, 20)
    a14 = atr(bars, 14)
    ph, pl = pivots(bars[-60:], 3)
    n = len(bars)
    print(f"\n===== {tf.upper()} =====")
    print(f"last 8 bars (t,o,h,l,c):")
    for b in bars[-8:]:
        print(f"  {b['t']}  o={b['o']:.2f} h={b['h']:.2f} l={b['l']:.2f} c={b['c']:.2f}")
    print(f"close={closes[-1]:.2f}  ema20={e20[-1]:.2f}  atr14={a14[-1]:.2f}")
    # 20-bar state
    above = sum(1 for i in range(-20, 0) if closes[i] > e20[i])
    hi = max(b["h"] for b in bars[-60:])
    lo = min(b["l"] for b in bars[-60:])
    print(f"60-bar range: {lo:.2f} – {hi:.2f}  (mid={(lo+hi)/2:.2f})")
    print(f"close vs ema20: {closes[-1]-e20[-1]:+.2f}  above20={above}/20")
    print(f"last 5 swings high: {ph[-5:]}")
    print(f"last 5 swings low:  {pl[-5:]}")
    # recent momentum: last 10 bars direction
    up = sum(1 for i in range(-10, 0) if closes[i] > closes[i - 1])
    print(f"last 10 bars up/down: {up}/{10-up}")
    # day range (last 24 for 1h, 96 for 15m etc)
    win = {"4h": 6, "1h": 24, "5m": 288}.get(tf, 24)
    win = min(win, n)
    dh = max(b["h"] for b in bars[-win:])
    dl = min(b["l"] for b in bars[-win:])
    print(f"{win}-bar range: {dl:.2f} – {dh:.2f} = {dh-dl:.2f} ({(dh-dl)/dl*100:.2f}%)")
