"""TV Indicator 6: Volume / Open Interest Footprint (Leviathan Capital)

逐价位成交量（或 ΔOI）剖面，按 K 线几何分配。

原版：`indicator("Volume / Open Interest Footprint - By Leviathan")`
数据：Volume 模式用 OHLCV；OI 模式用 OI 逐 bar 变化量 + OHLC。

与原版逐条对齐的要点：

  1. 档位自 `profHigh` **向下**排（`zoneBounds[i] = profHigh - gap*i`），
     不是从下往上
  2. `profHigh`/`profLow` 取 `ta.highest(...)[1]`——即**排除最后一根** bar
     的极值（原版用 `[1]` 是为了实时重绘时不抖）
  3. 成交量按 K 线几何分摊：实体权重 1、上下影线权重各 2
     （`bodyvol = body*v/(2*topwick+2*botwick+body)`）
  4. **影线的量一半记绿、一半记红**（影线方向不明）——实体量才按阴阳归属
  5. OI 模式**只用实体**、不用影线，且 `total` 只计增仓（`vpTotal = vpGreen`）
  6. `get_vol` 的 `nz(...)` 语义：区间无重叠 → 0；`height` 为 0 → 0（原版是 0/0=na）

未移植：多交易所成交量聚合（`aggr` + 8 个 `request.security`）——那需要同时
拉 8 个交易所的 volume，本项目按单所取数；以及全部绘图（box 剖面 / Positive
Delta 线 / 节点文字）。

另有两点与原版的**刻意差异**（原版在此会得到无意义结果，故收敛而非照搬）：
  - `body == 0`（doji）时原版 `bodyvol` 的分母可能为 0；此处跳过该 bar 的实体项
  - OI 模式下原版 `vpDelta`/`vpTotal` **仅在 `v > 0` 时更新**，于是「最后一根
    处理的是减仓 bar」时它们会停留在过时值；此处 `delta`/`total` 一律取最终
    累计值（`green - red` / OI 模式下 `green`）
"""

from __future__ import annotations

from typing import Optional

MODE_VOLUME = "volume"
MODE_OI = "oi"


def overlap_amount(y11: float, y12: float, y21: float, y22: float,
                   height: float, vol: float) -> float:
    """对齐原版 `get_vol`：两区间重叠长度 × `vol / height`。

    `height` 为 0 时原版得到 0/0 = na，`nz()` 转 0——这里直接返回 0。
    """
    lo1, hi1 = min(y11, y12), max(y11, y12)
    lo2, hi2 = min(y21, y22), max(y21, y22)
    ov = max(min(hi1, hi2) - max(lo1, lo2), 0.0)
    if height <= 0:
        return 0.0
    return ov * vol / height


def vol_oi_footprint(highs: list[float], lows: list[float],
                     opens: list[float], closes: list[float],
                     volumes: list[float],
                     resolution: int = 20, mode: str = MODE_VOLUME,
                     oi_values: Optional[list[float]] = None) -> dict:
    """计算 Volume / OI 逐价位剖面。

    `resolution` 对齐原版 `res`（默认 20）。
    `mode='oi'` 时必须提供与 OHLC 等长的 `oi_values`。
    """
    n = len(closes)
    if n < 3:
        return {"error": "not enough bars"}
    nr = max(2, int(resolution))
    if mode not in (MODE_VOLUME, MODE_OI):
        return {"error": f"unknown mode {mode!r}"}

    # 原版 profHigh/profLow = ta.highest/lowest(..., barsInSession+1)[1] → 排除最后一根
    prof_high = max(highs[:-1])
    prof_low = min(lows[:-1])
    if not prof_high > prof_low:
        return {"error": "flat price range (high == low)"}
    gap = (prof_high - prof_low) / nr

    if mode == MODE_OI:
        if not oi_values or len(oi_values) != n:
            return {"error": "oi_values required (same length) for mode='oi'"}
        vals: list[float] = [0.0]                       # 第一根无 ΔOI（原版为 na）
        for i in range(1, n):
            vals.append(oi_values[i] - oi_values[i - 1])
    else:
        vals = [float(v) for v in volumes]

    vp_green = [0.0] * nr
    vp_red = [0.0] * nr

    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        v = vals[i]
        if mode == MODE_OI and v == 0:
            continue                                    # 原版 v>0 / v<0 都不成立

        body_top = max(c, o)
        body_bot = min(c, o)
        is_green = c >= o
        top_wick = h - body_top
        bot_wick = body_bot - l
        body = body_top - body_bot

        if mode == MODE_OI:
            # 只用实体：green 计增仓、red 计减仓（原版不做影线分摊）
            for k in range(nr):
                z_top = prof_high - gap * k
                z_bot = z_top - gap
                if v > 0:
                    vp_green[k] += overlap_amount(z_bot, z_top, body_bot, body_top, body, v)
                else:
                    vp_red[k] += overlap_amount(z_bot, z_top, body_bot, body_top, body, -v)
            continue

        denom = 2.0 * top_wick + 2.0 * bot_wick + body
        if denom <= 0:
            continue                                    # 完全无波动 bar，原版会除零
        body_vol = body * v / denom
        top_vol = 2.0 * top_wick * v / denom
        bot_vol = 2.0 * bot_wick * v / denom

        for k in range(nr):
            z_top = prof_high - gap * k
            z_bot = z_top - gap
            body_part = overlap_amount(z_bot, z_top, body_bot, body_top, body, body_vol)
            # 影线：一半记绿、一半记红
            top_part = overlap_amount(z_bot, z_top, body_top, h, top_wick, top_vol) / 2.0
            bot_part = overlap_amount(z_bot, z_top, body_bot, l, bot_wick, bot_vol) / 2.0
            if is_green:
                vp_green[k] += body_part + top_part + bot_part
                vp_red[k] += top_part + bot_part
            else:
                vp_red[k] += body_part + top_part + bot_part
                vp_green[k] += top_part + bot_part

    levels = []
    for k in range(nr):
        z_top = prof_high - gap * k
        z_bot = z_top - gap
        # 原版 OI 模式 vpTotal = vpGreen（只计增仓）；Volume 模式 green + red
        total = vp_green[k] if mode == MODE_OI else vp_green[k] + vp_red[k]
        levels.append({
            "price_top": round(z_top, 6),
            "price_bot": round(z_bot, 6),
            "price_mid": round((z_top + z_bot) / 2.0, 6),
            "green": round(vp_green[k], 2),
            "red": round(vp_red[k], 2),
            "delta": round(vp_green[k] - vp_red[k], 2),
            "total": round(total, 2),
        })

    totals = {
        "green": round(sum(vp_green), 2),
        "red": round(sum(vp_red), 2),
        "delta": round(sum(vp_green) - sum(vp_red), 2),
        "total": round(sum(vp_green) if mode == MODE_OI else sum(vp_green) + sum(vp_red), 2),
    }

    poc_idx = max(range(nr), key=lambda k: levels[k]["total"])
    poc = {
        "level": poc_idx,
        "price_mid": levels[poc_idx]["price_mid"],
        "total": levels[poc_idx]["total"],
    }

    positive_delta_levels = [lv["price_mid"] for lv in levels if lv["delta"] > 0]

    return {
        "profile": {
            "high": round(prof_high, 6), "low": round(prof_low, 6),
            "resolution": nr, "step": round(gap, 6), "bars": n,
        },
        "mode": mode,
        "totals": totals,
        "dominant": "buyers" if totals["delta"] > 0 else "sellers",
        "poc": poc,
        "levels": levels,
        "positive_delta_levels": positive_delta_levels,
    }
