#!/usr/bin/env python3
"""用 ETH 15m 历史估算：动态 step 的分布 / 触发率 / 若调参后的差异。"""
import sqlite3
import statistics as st
from pathlib import Path

DB = Path("/opt/omnialpha/pa-data-source/data/kline.db")
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT t, c, atr14, boll20, boll20_upper, boll20_lower FROM kline "
    "WHERE symbol='ETH_USDT' AND interval='15m' AND closed=1 "
    "AND atr14 IS NOT NULL AND boll20 IS NOT NULL "
    "ORDER BY t DESC LIMIT 2000").fetchall()
con.close()
print(f"样本 {len(rows)} 根 15m（约 {len(rows)*15/60/24:.1f} 天）")

atr_pcts, bw_pcts = [], []
for t, c, atr, mid, up, lo in rows:
    if not c or not mid:
        continue
    atr_pcts.append(atr / c * 100)
    bw_pcts.append((up - lo) / mid * 100)


def pct(v, q):
    v = sorted(v)
    return v[int(len(v) * q) - 1] if v else 0


print("\nATR% 分布：P10 {:.3f}  P50 {:.3f}  P90 {:.3f}  max {:.3f}".format(
    pct(atr_pcts, .1), pct(atr_pcts, .5), pct(atr_pcts, .9), max(atr_pcts)))
print("带宽% 分布：P10 {:.3f}  P50 {:.3f}  P90 {:.3f}  max {:.3f}".format(
    pct(bw_pcts, .1), pct(bw_pcts, .5), pct(bw_pcts, .9), max(bw_pcts)))

for bb_div, atr_mult, floor, cap in ((6.0, 1.0, 0.45, 1.2), (4.0, 1.0, 0.45, 1.2),
                                     (6.0, 1.5, 0.45, 1.2), (4.0, 1.5, 0.45, 1.2)):
    raws = [max(a * atr_mult, b / bb_div) for a, b in zip(atr_pcts, bw_pcts)]
    eff = [min(cap, max(floor, r)) for r in raws]
    above = sum(1 for r in raws if r > floor) / len(raws) * 100
    lo_span = 7 * floor
    print(f"\nbb_div={bb_div} atr_mult={atr_mult} floor={floor}: "
          f"raw P50={pct(raws,.5):.3f} P90={pct(raws,.9):.3f} "
          f"触发(>floor) {above:.0f}% | 实际 step P50={pct(eff,.5):.3f} "
          f"P90={pct(eff,.9):.3f} | 7 档跨度 P90={7*pct(eff,.9):.2f}%")
