"""Smart Money Concepts (LuxAlgo) — 与原版 Pine 逐行对齐的计算层。

对齐基准：`Smart Money Concepts [LuxAlgo]` v5（Pine 源码）。

| 原版 Pine | 本文件 |
|---|---|
| `leg(size)`: `high[size] > ta.highest(size)` / `low[size] < ta.lowest(size)` | `_leg_series` |
| 枢轴 bar = 检测 bar − size，价取 `low[size]` / `high[size]` | `_update_pivot` |
| `getCurrentStructure` 只对 swing 更新 `trailing` | `_update_pivot` 的 `internal`/`eq` 分支 |
| `trailing.top/bottom` = 最后一个 swing 枢轴 | `_update_pivot` |
| Premium/Discount 区间 = `[trailing.bottom, trailing.top]` | 主循环尾部 |
| `displayStructure` 用**该 bar 时刻**的枢轴 | 单遍扫描（关键：不可拆成两遍） |
| `storeOrdeBlock`: `slice(pivotBarIndex, bar_index)` 左闭右开 | `_store_ob` |
| OB 上限 = `swingOrderBlocksSizeInput` / `internalOrderBlocksSizeInput` | `ob_limit` / `internal_ob_limit` |
| `deleteOrderBlocks` 每根 bar 都跑 | 主循环尾部 |
| `bearish/bullishOrderBlockMitigationSource` | `ob_mitigation` |
| `parsedHigh/parsedLow` 高波动 bar 交换 | 主循环前 |
| `volatilityMeasure`（Atr / Cumulative Mean Range） | `order_block_filter` |

有意偏离原版一处：`ta.atr(200)` 在数据不足 200 根时返回 `na`，原版此时 EQH/EQL
与 OB filter 静默失效。本实现改为自适应周期（`min(200, n)`）并在 warmup 段用累计
均值兜底，使 100–200 根数据同样可用；数据 ≥ 200 根时与原版完全一致。
实际周期见 `SMCMapResult.atr_period_used`，warmup 见 `atr_warmup`。

另注：原版 `leg()` 是**回顾式枢轴检测**（比较 `high[i-size]` 与它之后的 size 根），
不是「当前 bar 创 size 根新高」。写成后者会让窄幅震荡下永远检测不到枢轴。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

BULL = 1
BEAR = -1
BULLISH_LEG = 1
BEARISH_LEG = 0

SHOW_ALL = {
    "internals": True, "swings": True, "obs": True,
    "fvg": True, "eqh": True, "zones": True, "hl": True,
}


@dataclass
class Pivot:
    current: Optional[float] = None
    last: Optional[float] = None
    crossed: bool = False
    bar_time: int = 0
    bar_index: int = 0


@dataclass
class OrderBlock:
    bar_high: float
    bar_low: float
    bar_time: int
    bias: int
    bar_index: int = 0
    internal: bool = False
    mitigated_time: Optional[int] = None
    mitigated_bar: Optional[int] = None


@dataclass
class FVG:
    top: float
    bottom: float
    bias: int
    left_time: int
    right_time: int
    bar_index: int = 0
    mitigated_time: Optional[int] = None
    mitigated_bar: Optional[int] = None


@dataclass
class SMCMapResult:
    swing_trend: int = 0
    internal_trend: int = 0
    events: list = field(default_factory=list)
    swing_order_blocks: list = field(default_factory=list)
    internal_order_blocks: list = field(default_factory=list)
    equal_highs: list = field(default_factory=list)
    equal_lows: list = field(default_factory=list)
    fvgs: list = field(default_factory=list)
    trailing: dict = field(default_factory=dict)
    premium_discount: dict = field(default_factory=dict)
    mtf_levels: dict = field(default_factory=dict)
    swing_pivots: list = field(default_factory=list)
    structure_scale: dict = field(default_factory=dict)
    atr: Optional[float] = None
    atr_period_used: int = 0
    atr_degraded: bool = False
    drawings: dict = field(default_factory=dict)


def _tr(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _rma(values: list[float], period: int) -> list[Optional[float]]:
    """Pine `ta.rma`：alpha = 1/period，首值 = SMA(period)。"""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def _atr_series(highs, lows, closes, period: int):
    """`ta.atr(period)`。

    Pine 在 bar 数不足 period 时返回 na（前 period-1 根恒为 na）。这里把周期收缩
    到数据长度、并在 warmup 段用累计均值兜底，使短历史同样可用。返回的 `degraded`
    表示是否发生了这种收缩 —— 数据 ≥ period 根时逐值与原版一致（degraded=False）。
    """
    n = len(highs)
    trs = [_tr(highs[i], lows[i], closes[i - 1] if i else closes[i]) for i in range(n)]
    want = max(1, int(period))
    used = max(1, min(want, n))
    rma = _rma(trs, used)
    degraded = used < want
    cum = 0.0
    out: list[Optional[float]] = []
    for i in range(n):
        cum += trs[i]
        if rma[i] is not None:
            out.append(rma[i])
        elif degraded:
            out.append(cum / (i + 1))  # 数据不足才兜底
        else:
            out.append(None)  # 数据充足 → 与原版一致，前 period-1 根为 na
    return out, used, degraded


def _leg_series(highs, lows, size: int) -> list[int]:
    """原版 `leg(size)`。

    `newLegHigh = high[size] > ta.highest(size)`，而 `ta.highest(size)` 在当前 bar
    取 `[i-size+1, i]`；因此比较的是 bar `i-size` 与它之后的 size 根（含当前 bar）。
    这是回顾式枢轴检测 —— 若写成 `highs[i] > max(highs[i-size:i])`（当前 bar 创
    size 根新高）会在窄幅震荡里永远不触发。
    """
    n = len(highs)
    leg = [BEARISH_LEG] * n
    if size < 1:
        return leg
    state = BEARISH_LEG
    for i in range(n):
        if i < size:
            leg[i] = state
            continue
        new_high = highs[i - size] > max(highs[i - size + 1: i + 1])
        new_low = lows[i - size] < min(lows[i - size + 1: i + 1])
        if new_high:
            state = BEARISH_LEG
        elif new_low:
            state = BULLISH_LEG
        leg[i] = state
    return leg


def compute_smc_map(
    rows: list[dict],
    *,
    swings_length: int = 50,
    internal_size: int = 5,
    eqh_len: int = 3,
    eqh_threshold_atr_mult: float = 0.1,
    atr_period: int = 200,
    order_block_filter: str = "atr",
    ob_mitigation: str = "highlow",
    ob_limit: int = 5,
    internal_ob_limit: int = 5,
    fvg_enabled: bool = True,
    fvg_auto_threshold: bool = True,
    fvg_extend: int = 1,
    internal_confluence: bool = False,
    htf_levels: Optional[dict] = None,
    show: Optional[dict] = None,
) -> SMCMapResult:
    """rows: `[{t,o,h,l,c,v}, ...]` 时间升序。"""
    show = {**SHOW_ALL, **(show or {})}
    n = len(rows)
    res = SMCMapResult()
    res.drawings = {"labels": [], "lines": [], "boxes": []}
    if n < 5:
        return res

    highs = [float(r["h"]) for r in rows]
    lows = [float(r["l"]) for r in rows]
    closes = [float(r["c"]) for r in rows]
    opens = [float(r.get("o") if r.get("o") is not None else r["c"]) for r in rows]
    times = [int(r.get("t") or i) for i, r in enumerate(rows)]

    atr, atr_used, atr_degraded = _atr_series(highs, lows, closes, atr_period)
    res.atr = atr[-1]
    res.atr_period_used = atr_used
    res.atr_degraded = atr_degraded

    # volatilityMeasure = orderBlockFilterInput == ATR ? atrMeasure : ta.cum(ta.tr)/bar_index
    if str(order_block_filter).lower() == "range":
        vol: list[float] = []
        cum_tr = 0.0
        for i in range(n):
            cum_tr += _tr(highs[i], lows[i], closes[i - 1] if i else closes[i])
            vol.append(cum_tr / (i + 1))
    else:
        vol = atr

    # parsedHigh / parsedLow：高波动 bar 交换 high/low
    parsed_high = [0.0] * n
    parsed_low = [0.0] * n
    for i in range(n):
        v = vol[i]
        # vol 为 na 时原版比较结果为 na（按 false 分支处理）→ 不做 high/low 交换
        high_vol_bar = v is not None and (highs[i] - lows[i]) >= (2 * v)
        parsed_high[i] = lows[i] if high_vol_bar else highs[i]
        parsed_low[i] = highs[i] if high_vol_bar else lows[i]

    # OB mitigation source：Close 或 High/Low
    mit = str(ob_mitigation).lower()
    bear_src = closes if mit == "close" else highs
    bull_src = closes if mit == "close" else lows

    leg_swing = _leg_series(highs, lows, swings_length)
    leg_inner = _leg_series(highs, lows, internal_size)
    leg_eq = _leg_series(highs, lows, eqh_len)

    swing_hi, swing_lo = Pivot(), Pivot()
    inner_hi, inner_lo = Pivot(), Pivot()
    eq_hi, eq_lo = Pivot(), Pivot()

    swing_trend = 0
    inner_trend = 0

    trailing_top: Optional[float] = None
    trailing_bottom: Optional[float] = None
    trailing_top_t = 0
    trailing_bottom_t = 0
    trailing_top_i = 0
    trailing_bottom_i = 0

    swing_obs: list[OrderBlock] = []
    inner_obs: list[OrderBlock] = []
    fvgs: list[FVG] = []
    events: list[dict] = []
    swing_pivots: list[dict] = []

    def push_event(kind: str, i: int, **kw):
        events.append({"bar_index": i, "time": times[i], "type": kind, **kw})

    # ---- 原版 getCurrentStructure：逐 bar 更新枢轴 ----
    def update_pivot(leg, i, size, piv_lo: Pivot, piv_hi: Pivot, *, internal: bool, eq: bool):
        nonlocal trailing_top, trailing_bottom, trailing_top_t, trailing_bottom_t
        nonlocal trailing_top_i, trailing_bottom_i
        chg = leg[i] - leg[i - 1]
        if chg == 0:
            return
        j = i - size
        if j < 0:
            return
        if chg > 0:  # startOfBullishLeg → 低点枢轴
            level = lows[j]
            if eq and piv_lo.current is not None:
                _a = atr[i]
                thr = eqh_threshold_atr_mult * _a if _a is not None else 0.0
                if thr > 0 and abs(level - piv_lo.current) < thr:
                    res.equal_lows.append({"bar": j, "time": times[j], "price": level,
                                           "prev": piv_lo.current, "prev_bar": piv_lo.bar_index})
                    push_event("equal_lows", j, price=level, prev=piv_lo.current)
                    res.drawings["labels"].append({"x": j, "y": level, "text": "EQL", "side": "up"})
            piv_lo.last = piv_lo.current
            piv_lo.current = level
            piv_lo.crossed = False
            piv_lo.bar_time = times[j]
            piv_lo.bar_index = j
            if not internal and not eq:
                trailing_bottom = level
                trailing_bottom_t = times[j]
                trailing_bottom_i = j
                swing_pivots.append({"bar": j, "time": times[j], "kind": "low", "price": level})
        else:  # startOfBearishLeg → 高点枢轴
            level = highs[j]
            if eq and piv_hi.current is not None:
                _a = atr[i]
                thr = eqh_threshold_atr_mult * _a if _a is not None else 0.0
                if thr > 0 and abs(level - piv_hi.current) < thr:
                    res.equal_highs.append({"bar": j, "time": times[j], "price": level,
                                            "prev": piv_hi.current, "prev_bar": piv_hi.bar_index})
                    push_event("equal_highs", j, price=level, prev=piv_hi.current)
                    res.drawings["labels"].append({"x": j, "y": level, "text": "EQH", "side": "down"})
            piv_hi.last = piv_hi.current
            piv_hi.current = level
            piv_hi.crossed = False
            piv_hi.bar_time = times[j]
            piv_hi.bar_index = j
            if not internal and not eq:
                trailing_top = level
                trailing_top_t = times[j]
                trailing_top_i = j
                swing_pivots.append({"bar": j, "time": times[j], "kind": "high", "price": level})

    # ---- 原版 storeOrdeBlock ----
    def store_ob(piv: Pivot, internal: bool, bias: int, i: int):
        s = max(0, piv.bar_index)
        e = i  # 原版 slice(pivotBarIndex, bar_index)：左闭右开
        if e <= s:
            return
        if bias == BEAR:
            k = max(range(s, e), key=lambda j: parsed_high[j])
        else:
            k = min(range(s, e), key=lambda j: parsed_low[j])
        ob = OrderBlock(bar_high=parsed_high[k], bar_low=parsed_low[k],
                        bar_time=times[k], bias=bias, bar_index=k, internal=internal)
        arr = inner_obs if internal else swing_obs
        limit = max(1, int(internal_ob_limit if internal else ob_limit))
        arr.insert(0, ob)
        while len(arr) > limit:
            arr.pop()
        res.drawings["boxes"].append({
            "type": "order_block", "internal": internal,
            "bias": "bull" if bias == BULL else "bear",
            "x1": times[k], "y1": ob.bar_high, "x2": times[-1], "y2": ob.bar_low,
        })

    # ---- 原版 displayStructure：用「该 bar 时刻」的枢轴 ----
    def display_structure(i: int, piv_lo: Pivot, piv_hi: Pivot, *, internal: bool):
        nonlocal swing_trend, inner_trend
        cur_trend = inner_trend if internal else swing_trend

        if piv_hi.current is not None and not piv_hi.crossed and closes[i] > piv_hi.current:
            tag = "CHoCH" if cur_trend == BEAR else "BOS"
            piv_hi.crossed = True
            if internal:
                inner_trend = BULL
            else:
                swing_trend = BULL
            filtered = internal and internal_confluence and swing_trend not in (0, BULL)
            if not filtered:
                push_event(("internal_" if internal else "swing_") + tag.lower(), i,
                           direction="bull", tag=tag, level=piv_hi.current)
                res.drawings["lines"].append({
                    "x1": piv_hi.bar_index, "y1": piv_hi.current, "x2": i, "y2": piv_hi.current,
                    "style": "dashed" if internal else "solid", "color": "green", "tag": tag,
                })
            if show.get("obs"):
                store_ob(piv_lo, internal, BULL, i)

        if piv_lo.current is not None and not piv_lo.crossed and closes[i] < piv_lo.current:
            tag = "CHoCH" if cur_trend == BULL else "BOS"
            piv_lo.crossed = True
            if internal:
                inner_trend = BEAR
            else:
                swing_trend = BEAR
            filtered = internal and internal_confluence and swing_trend not in (0, BEAR)
            if not filtered:
                push_event(("internal_" if internal else "swing_") + tag.lower(), i,
                           direction="bear", tag=tag, level=piv_lo.current)
                res.drawings["lines"].append({
                    "x1": piv_lo.bar_index, "y1": piv_lo.current, "x2": i, "y2": piv_lo.current,
                    "style": "dashed" if internal else "solid", "color": "red", "tag": tag,
                })
            if show.get("obs"):
                store_ob(piv_hi, internal, BEAR, i)

    # FVG auto threshold：ta.cum(|close - close[2]|) / bar_index
    fvg_thr: list[float] = []
    if fvg_auto_threshold:
        cum_abs = 0.0
        for i in range(n):
            if i >= 2:
                cum_abs += abs(closes[i] - closes[i - 2])
            fvg_thr.append(cum_abs / (i + 1) if i else 0.0)

    for i in range(1, n):
        # 1) 枢轴（先 swing，internal_confluence 依赖它）
        update_pivot(leg_swing, i, swings_length, swing_lo, swing_hi, internal=False, eq=False)
        if show.get("internals") or show.get("obs"):
            update_pivot(leg_inner, i, internal_size, inner_lo, inner_hi, internal=True, eq=False)
        if show.get("eqh"):
            update_pivot(leg_eq, i, eqh_len, eq_lo, eq_hi, internal=False, eq=True)

        # 2) 结构突破
        if show.get("swings") or show.get("obs"):
            display_structure(i, swing_lo, swing_hi, internal=False)
        if show.get("internals") or show.get("obs"):
            display_structure(i, inner_lo, inner_hi, internal=True)

        # 3) FVG（3 根）
        if fvg_enabled and i >= 2:
            if lows[i] > highs[i - 2] and closes[i - 1] > highs[i - 2]:
                gap = lows[i] - highs[i - 2]
                if (not fvg_auto_threshold) or gap > fvg_thr[i]:
                    fvgs.append(FVG(top=lows[i], bottom=highs[i - 2], bias=BULL,
                                    left_time=times[i - 2], right_time=times[i], bar_index=i - 2))
                    push_event("bullish_fvg", i, top=lows[i], bottom=highs[i - 2])
                    res.drawings["boxes"].append({
                        "type": "fvg", "bias": "bull", "x1": times[i - 2],
                        "y1": lows[i], "x2": times[min(n - 1, i + fvg_extend)], "y2": highs[i - 2],
                    })
            if highs[i] < lows[i - 2] and closes[i - 1] < lows[i - 2]:
                gap = lows[i - 2] - highs[i]
                if (not fvg_auto_threshold) or gap > fvg_thr[i]:
                    fvgs.append(FVG(top=lows[i - 2], bottom=highs[i], bias=BEAR,
                                    left_time=times[i - 2], right_time=times[i], bar_index=i - 2))
                    push_event("bearish_fvg", i, top=lows[i - 2], bottom=highs[i])
                    res.drawings["boxes"].append({
                        "type": "fvg", "bias": "bear", "x1": times[i - 2],
                        "y1": lows[i - 2], "x2": times[min(n - 1, i + fvg_extend)], "y2": highs[i],
                    })

        # 4) OB mitigation（原版 deleteOrderBlocks，每根 bar 跑）
        for arr, internal in ((swing_obs, False), (inner_obs, True)):
            keep = []
            for ob in arr:
                hit = ((ob.bias == BEAR and bear_src[i] > ob.bar_high) or
                       (ob.bias == BULL and bull_src[i] < ob.bar_low))
                if hit:
                    ob.mitigated_time = times[i]
                    ob.mitigated_bar = i
                    push_event("ob_mitigated", i,
                               bias="bear" if ob.bias == BEAR else "bull",
                               internal=internal, high=ob.bar_high, low=ob.bar_low)
                    continue
                keep.append(ob)
            arr[:] = keep

        # 5) FVG mitigation（close 穿过即失效）
        keep_f = []
        for f in fvgs:
            hit = ((f.bias == BULL and closes[i] < f.bottom) or
                   (f.bias == BEAR and closes[i] > f.top))
            if hit:
                f.mitigated_time = times[i]
                f.mitigated_bar = i
                push_event("fvg_mitigated", i, bias="bull" if f.bias == BULL else "bear")
                continue
            keep_f.append(f)
        fvgs[:] = keep_f

    res.swing_trend = swing_trend
    res.internal_trend = inner_trend
    res.events = events
    res.swing_order_blocks = swing_obs
    res.internal_order_blocks = inner_obs
    res.fvgs = fvgs
    res.swing_pivots = swing_pivots
    res.mtf_levels = dict(htf_levels or {})

    # trailing：原版只由 swing 枢轴更新
    res.trailing = {
        "top": trailing_top, "bottom": trailing_bottom,
        "top_time": trailing_top_t, "bottom_time": trailing_bottom_t,
        "top_bar": trailing_top_i, "bottom_bar": trailing_bottom_i,
    }

    # Premium / Discount：区间 = [trailing.bottom, trailing.top]（不是全历史极值）
    if trailing_top is not None and trailing_bottom is not None:
        pd_top = max(trailing_top, trailing_bottom)
        pd_bottom = min(trailing_top, trailing_bottom)
        pd_source = "swing_range"
    else:
        pd_top, pd_bottom = max(highs), min(lows)
        pd_source = "full_history"
    eq = (pd_top + pd_bottom) / 2
    res.premium_discount = {
        "premium_top": pd_top,
        "premium_bottom": 0.95 * pd_top + 0.05 * pd_bottom,
        "equilibrium": eq,
        "eq_band_top": 0.525 * pd_top + 0.475 * pd_bottom,
        "eq_band_bottom": 0.525 * pd_bottom + 0.475 * pd_top,
        "discount_top": 0.95 * pd_bottom + 0.05 * pd_top,
        "discount_bottom": pd_bottom,
        "current_zone": "premium" if closes[-1] > eq else "discount",
        "source": pd_source,
    }

    # 结构尺度：让模型有真实可引用的波动率，不必外求指标
    ob_heights = [abs(o.bar_high - o.bar_low) for o in (swing_obs + inner_obs)]
    fvg_widths = [abs(f.top - f.bottom) for f in fvgs]
    recent = min(20, n)
    recent_ranges = [highs[k] - lows[k] for k in range(n - recent, n)]
    res.structure_scale = {
        "atr": round(atr[-1], 4) if atr[-1] is not None else None,
        "atr_period_used": atr_used,
        "atr_degraded": atr_degraded,
        "swing_range": round(pd_top - pd_bottom, 4),
        "swing_range_source": pd_source,
        "ob_height_avg": round(sum(ob_heights) / len(ob_heights), 4) if ob_heights else None,
        "fvg_width_avg": round(sum(fvg_widths) / len(fvg_widths), 4) if fvg_widths else None,
        "recent_bar_range_avg": round(sum(recent_ranges) / len(recent_ranges), 4),
        "recent_bars": recent,
    }

    if show.get("zones"):
        res.drawings["boxes"].append({"type": "premium", "x1": times[0],
                                      "y1": pd_top, "x2": times[-1],
                                      "y2": 0.95 * pd_top + 0.05 * pd_bottom})
        res.drawings["boxes"].append({"type": "discount", "x1": times[0],
                                      "y1": 0.95 * pd_bottom + 0.05 * pd_top, "x2": times[-1],
                                      "y2": pd_bottom})

    if show.get("hl") and trailing_top is not None and trailing_bottom is not None:
        res.drawings["labels"].append({
            "x": n - 1, "y": trailing_top,
            "text": "Strong High" if swing_trend == BEAR else "Weak High", "side": "down"})
        res.drawings["labels"].append({
            "x": n - 1, "y": trailing_bottom,
            "text": "Strong Low" if swing_trend == BULL else "Weak Low", "side": "up"})

    return res


def smc_map_summary(res: SMCMapResult) -> dict:
    """Compact JSON for LLM tools."""

    def _ob(o: OrderBlock) -> dict:
        return {"bias": "bull" if o.bias == BULL else "bear",
                "high": o.bar_high, "low": o.bar_low,
                "time": o.bar_time, "bar": o.bar_index}

    return {
        "swing_trend": {1: "bull", -1: "bear", 0: "neutral"}.get(res.swing_trend, "neutral"),
        "internal_trend": {1: "bull", -1: "bear", 0: "neutral"}.get(res.internal_trend, "neutral"),
        "events": res.events[-12:],
        "order_blocks": {
            "swing": [_ob(o) for o in res.swing_order_blocks[:5]],
            "internal": [_ob(o) for o in res.internal_order_blocks[:5]],
        },
        "equal_highs": res.equal_highs[-3:],
        "equal_lows": res.equal_lows[-3:],
        "fvgs": [{"bias": "bull" if f.bias == BULL else "bear", "top": f.top,
                  "bottom": f.bottom, "time": f.left_time, "bar": f.bar_index}
                 for f in res.fvgs[-5:]],
        "premium_discount": res.premium_discount,
        "trailing": res.trailing,
        "swing_pivots": res.swing_pivots[-6:],
        "structure_scale": res.structure_scale,
        "mtf_levels": res.mtf_levels,
    }
