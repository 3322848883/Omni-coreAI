"""TV Indicator 5: OI Visible Range (Kioseff Trading)

持仓量四象限 + 价格档位剖面。

原版：`indicator("OI Visible Range [Kioseff Trading]")`
数据：OI 逐 bar 变化量 + OHLC（OI 序列走 Gate `/contract_stats?interval=<tf>`）。

四象限（原版 4 个 row 的语义，`effSwitchReg` 的写入目标）：

| 价格 | ΔOI | 语义 | 原版 row |
|---|---|---|---|
| 涨 | 增 | Buyers Entered（多头新开仓） | row1 |
| 涨 | 减 | Sellers Exited（空头回补） | row2 |
| 跌 | 增 | Sellers Entered（空头新开仓） | row3 |
| 跌 | 减 | Buyers Exited（多头平仓） | row4 |

移植约定（逐条对齐原版，不自行「优化」）：
  - 档位是 `rows` 个等距**价格点**（含两端），不是区间——`setLevels`
  - ΔOI 按 bar 的 high/low 跨越的档位数**均摊**：`div = |lx1 - lx2| + 1`
  - `lx1`/`lx2` 用 `binary_search_leftmost` 语义（high 与 low 各自落点）
  - ΔOI 为 0 或 `close == open` 的 bar 不计入（原版 switch 无匹配分支）
  - Value Area 从 POC 向两侧**对称**扩展至 `va_pct`%——原版 `findValue`
  - 原版所有 `for a to b` 循环方向无关（Pine 自动递增/递减），故统一走 `_span`
  - 原版的绘图部分（`lineDraw`/`drawPOC`/`createPoly`）在源码里本就整段注释掉了，
    未移植；`findValue` 仍在跑，故 VA 作为数值保留

两处防御性收敛（正常数据下不会触发，仅为避免越界/除零）：
  - `_leftmost`/`_rightmost` 的结果 clamp 到 `[0, rows-1]`；原版越界时会返回 `size`
  - `rows < 2` 退化为单档；原版 `ROWS` 的 input 下限是 7
"""

from __future__ import annotations

import bisect
from typing import Optional

QUADRANTS = ("buyers_entered", "sellers_exited", "sellers_entered", "buyers_exited")


def _levels(lo: float, hi: float, rows: int) -> list[float]:
    """原版 `setLevels`：`rows` 个等距价格点（含 min 与 max 两端）。"""
    if rows < 2:
        return [lo]
    step = abs(hi - lo) / (rows - 1)
    return [lo + step * i for i in range(rows)]


def _leftmost(sorted_arr: list[float], val: float) -> int:
    """对齐 Pine `array.binary_search_leftmost`：第一个 `>= val` 的索引。"""
    return min(bisect.bisect_left(sorted_arr, val), len(sorted_arr) - 1)


def _rightmost(sorted_arr: list[float], val: float) -> int:
    """对齐 Pine `array.binary_search_rightmost`：最后一个 `<= val` 的索引。"""
    return max(bisect.bisect_right(sorted_arr, val) - 1, 0)


def _span(a: int, b: int) -> range:
    """对齐 Pine `for i = a to b`：按方向自动递增/递减，总是覆盖 [min, max]。

    这一点必须显式处理：`effSwitchReg` 里 `lx1 >= lx2`（high 落点不低于 low），
    而 `calcDelta` 里 `lx1 <= lx2`（`rightmost(c1)` 必然 <= `leftmost(c2)`）——
    两处方向相反，写死 `range(lx2, lx1+1)` 会让 Summed 聚合整段落空。
    """
    return range(min(a, b), max(a, b) + 1)


def _value_area(vals: list[float], va_pct: float) -> tuple[Optional[int], Optional[int]]:
    """原版 `findValue`：从最大值位置向两侧对称扩展，直到累计占比 >= va_pct。"""
    total = sum(vals)
    if total <= 0:
        return None, None
    highest = vals.index(max(vals))
    n = len(vals)
    for i in range(1, n):
        lo_i = max(highest - i, 0)
        hi_i = min(highest + i + 1, n)
        if sum(vals[lo_i:hi_i]) / total >= va_pct / 100.0:
            return lo_i, min(highest + i, n - 1)
    return 0, n - 1


def oi_visible_range(highs: list[float], lows: list[float],
                     opens: list[float], closes: list[float],
                     oi_values: list[float],
                     rows: int = 20, va_pct: float = 70.0,
                     delta_rows: int = 50) -> dict:
    """计算 OI 四象限剖面。

    `oi_values` 是与 OHLC 已对齐的持仓量序列（同长度、同时刻）。
    rows 对齐原版 `ROWS`（默认 20），va_pct 对齐 `cumu`（默认 70），
    delta_rows 对齐 `deltaRows`（默认 50，非 merge 模式上限 125）。
    """
    n = len(closes)
    if n < 3:
        return {"error": "not enough bars"}
    if len(oi_values) != n:
        return {"error": "oi series length mismatch"}

    nr = max(2, int(rows))

    # 原版 oi = close - close[1]（在 OI 符号上）→ 第一根为 na，跳过
    delta_oi: list[Optional[float]] = [None]
    for i in range(1, n):
        delta_oi.append(oi_values[i] - oi_values[i - 1])

    lo = min(lows)
    hi = max(highs)
    if not hi > lo:
        return {"error": "flat price range (high == low)"}
    levels = _levels(lo, hi, nr)

    q: dict[str, list[float]] = {k: [0.0] * nr for k in QUADRANTS}

    for i in range(n):
        d = delta_oi[i]
        if d is None or d == 0:
            continue                      # retArr == 0 → 原版 switch 无匹配
        if closes[i] == opens[i]:
            continue                      # priceArr == 0 → 原版两个 if 都不进

        lx1 = _leftmost(levels, highs[i])
        lx2 = _leftmost(levels, lows[i])
        div = abs(lx1 - lx2) + 1
        amt = abs(d / div)

        if closes[i] > opens[i]:
            key = "buyers_entered" if d > 0 else "sellers_exited"
        else:
            key = "sellers_entered" if d > 0 else "buyers_exited"

        for x in _span(lx1, lx2):
            q[key][x] += amt

    total_all = sum(sum(v) for v in q.values())
    dominant = max(QUADRANTS, key=lambda k: sum(q[k]))

    quadrants = {}
    for k in QUADRANTS:
        vals = q[k]
        tot = sum(vals)
        poc = vals.index(max(vals)) if tot > 0 else None
        va_lo, va_hi = _value_area(vals, va_pct)
        quadrants[k] = {
            "total": round(tot, 2),
            "share_pct": None if total_all <= 0 else round(tot / total_all * 100.0, 2),
            "poc_level": poc,
            "poc_price": None if poc is None else round(levels[poc], 6),
            "va_low_price": None if va_lo is None else round(levels[va_lo], 6),
            "va_high_price": None if va_hi is None else round(levels[va_hi], 6),
        }

    level_rows = []
    for i in range(nr):
        level_rows.append({
            "price": round(levels[i], 6),
            "buyers_entered": round(q["buyers_entered"][i], 2),
            "sellers_exited": round(q["sellers_exited"][i], 2),
            "sellers_entered": round(q["sellers_entered"][i], 2),
            "buyers_exited": round(q["buyers_exited"][i], 2),
        })

    # Summed OI（原版 calcDelta）：把价格档位再按 delta_rows 个区间聚合四类值
    dr = max(2, min(int(delta_rows), 125))
    dstep = abs(hi - lo) / dr
    summed = []
    for i in range(dr):
        c1 = lo + dstep * i
        c2 = lo + dstep * (i + 1)
        lx1 = _rightmost(levels, c1)
        lx2 = _leftmost(levels, c2)
        s = {k: 0.0 for k in QUADRANTS}
        for x in _span(lx1, lx2):
            for k in QUADRANTS:
                s[k] += q[k][x]
        tot = sum(s.values())
        if tot <= 0:
            continue
        summed.append({
            "price_low": round(c1, 6),
            "price_high": round(c2, 6),
            "total": round(tot, 2),
            "buyers_entered": round(s["buyers_entered"], 2),
            "sellers_exited": round(s["sellers_exited"], 2),
            "sellers_entered": round(s["sellers_entered"], 2),
            "buyers_exited": round(s["buyers_exited"], 2),
        })
    summed.sort(key=lambda r: -r["total"])

    return {
        "profile": {
            "low": round(lo, 6), "high": round(hi, 6),
            "rows": nr, "step": round((hi - lo) / (nr - 1), 6) if nr > 1 else 0.0,
            "bars": n,
        },
        "quadrants": quadrants,
        "dominant": dominant,
        "total_abs_oi_change": round(total_all, 2),
        "levels": level_rows,
        "summed": {
            "rows": dr,
            "step": round(dstep, 6),
            "top": summed[:8],
            "nonempty_buckets": len(summed),
        },
    }
