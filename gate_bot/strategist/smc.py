"""Smart Money Concepts (LuxAlgo-inspired) — computation + draw primitives.

Returns analysis + drawing payloads (boxes/lines/labels) for a future frontend.
No TV drawing here; numbers must match the Pine logic on the same OHLC series.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

BULL = 1
BEAR = -1
BULLISH_LEG = 1
BEARISH_LEG = 0


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
    bias: int  # BULL/BEAR
    bar_index: int = 0


@dataclass
class FVG:
    top: float
    bottom: float
    bias: int
    left_time: int
    right_time: int


@dataclass
class SMCResult:
    # trends
    swing_trend: int = 0
    internal_trend: int = 0
    # events (per bar index)
    events: list = field(default_factory=list)
    # zones
    swing_order_blocks: list = field(default_factory=list)
    internal_order_blocks: list = field(default_factory=list)
    equal_highs: list = field(default_factory=list)
    equal_lows: list = field(default_factory=list)
    fvgs: list = field(default_factory=list)
    trailing: dict = field(default_factory=dict)
    premium_discount: dict = field(default_factory=dict)
    mtf_levels: dict = field(default_factory=dict)
    # draw primitives for frontend
    drawings: dict = field(default_factory=dict)


def _tr(high, low, prev_close):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_smc(
    rows: list[dict],
    *,
    swings_length: int = 50,
    internal_size: int = 5,
    eqh_len: int = 3,
    eqh_threshold_atr_mult: float = 0.1,
    atr_period: int = 200,
    order_block_filter: str = "atr",  # atr | range
    show: Optional[dict] = None,
) -> SMCResult:
    """rows: [{t,o,h,l,c,v}, ...] chronological.

    show: optional subset flags e.g. {"internals":True,"swings":True,"obs":True,"fvg":True,"eqh":True,"zones":True}
    """
    show = show or {"internals": True, "swings": True, "obs": True, "fvg": True, "eqh": True, "zones": True, "hl": True}
    n = len(rows)
    res = SMCResult()
    if n < 5:
        return res

    highs = [float(r["h"]) for r in rows]
    lows = [float(r["l"]) for r in rows]
    closes = [float(r["c"]) for r in rows]
    times = [int(r.get("t") or i) for i, r in enumerate(rows)]

    # ATR
    atr = [None] * n
    trs = []
    for i in range(n):
        pc = closes[i - 1] if i else closes[i]
        trs.append(_tr(highs[i], lows[i], pc))
    if n >= atr_period:
        atr[atr_period - 1] = sum(trs[:atr_period]) / atr_period
        for i in range(atr_period, n):
            atr[i] = (atr[i - 1] * (atr_period - 1) + trs[i]) / atr_period
    else:
        atr[-1] = sum(trs) / len(trs)

    def vol(i: int) -> float:
        if order_block_filter == "range":
            return sum(trs[: i + 1]) / (i + 1)
        return atr[i] if atr[i] is not None else atr[-1]

    # parsed high/low (high-vol bar swap)
    parsed_high = [0.0] * n
    parsed_low = [0.0] * n
    for i in range(n):
        hv = (highs[i] - lows[i]) >= (2 * vol(i))
        parsed_high[i] = lows[i] if hv else highs[i]
        parsed_low[i] = highs[i] if hv else lows[i]

    # leg detection (size-window)
    def leg_series(size: int):
        leg = [0] * n
        state = 0
        for i in range(n):
            if i < size:
                leg[i] = state
                continue
            new_high = highs[i] > max(highs[i - size : i])
            new_low = lows[i] < min(lows[i - size : i])
            if new_high:
                state = BEARISH_LEG
            elif new_low:
                state = BULLISH_LEG
            leg[i] = state
        return leg

    swing_leg = leg_series(swings_length)
    inner_leg = leg_series(internal_size)
    eq_leg = leg_series(eqh_len)

    swing_high = Pivot()
    swing_low = Pivot()
    internal_high = Pivot()
    internal_low = Pivot()
    equal_high = Pivot()
    equal_low = Pivot()
    swing_trend = 0
    internal_trend = 0
    trailing_top = None
    trailing_bottom = None
    trailing_top_t = 0
    trailing_bottom_t = 0
    trailing_top_i = 0
    trailing_bottom_i = 0

    drawings = {"labels": [], "lines": [], "boxes": []}
    events = []

    def push_event(kind: str, i: int, **kw):
        events.append({"bar_index": i, "time": times[i], "type": kind, **kw})

    def handle_structure(size_leg, low_piv: Pivot, high_piv: Pivot, internal: bool, eq: bool = False):
        nonlocal trailing_top, trailing_bottom, trailing_top_t, trailing_bottom_t
        nonlocal trailing_top_i, trailing_bottom_i
        for i in range(1, n):
            chg = size_leg[i] - size_leg[i - 1]
            if chg == 0:
                continue
            # bullish leg start = new low pivot
            if chg > 0:
                level = lows[i - size_leg and i or i] if False else lows[i]
                # Pine uses low[size] at pivot bar — approximate with window extreme at change bar
                window = list(range(max(0, i - 1), i + 1))
                level = min(lows[j] for j in window)
                low_piv.last = low_piv.current
                low_piv.current = level
                low_piv.crossed = False
                low_piv.bar_time = times[i]
                low_piv.bar_index = i
                if not internal and not eq:
                    trailing_bottom = level
                    trailing_bottom_t = times[i]
                    trailing_bottom_i = i
                if eq:
                    if low_piv.last is not None and abs(low_piv.current - level) < (eqh_threshold_atr_mult or 0.1) * (atr[i] or 1):
                        res.equal_lows.append({"bar": i, "price": level, "prev": low_piv.last, "time": times[i]})
                        drawings["labels"].append({"x": i, "y": level, "text": "EQL", "side": "up"})
                        push_event("equal_lows", i, price=level)
            elif chg < 0:
                window = list(range(max(0, i - 1), i + 1))
                level = max(highs[j] for j in window)
                high_piv.last = high_piv.current
                high_piv.current = level
                high_piv.crossed = False
                high_piv.bar_time = times[i]
                high_piv.bar_index = i
                if not internal and not eq:
                    trailing_top = level
                    trailing_top_t = times[i]
                    trailing_top_i = i
                if eq:
                    if high_piv.last is not None and abs(high_piv.current - level) < (eqh_threshold_atr_mult or 0.1) * (atr[i] or 1):
                        res.equal_highs.append({"bar": i, "price": level, "prev": high_piv.last, "time": times[i]})
                        drawings["labels"].append({"x": i, "y": level, "text": "EQH", "side": "down"})
                        push_event("equal_highs", i, price=level)

    handle_structure(swing_leg, swing_low, swing_high, internal=False)
    handle_structure(inner_leg, internal_low, internal_high, internal=True)
    if show.get("eqh"):
        handle_structure(eq_leg, equal_low, equal_high, internal=False, eq=True)

    def display_structure(low_piv: Pivot, high_piv: Pivot, internal: bool):
        nonlocal swing_trend, internal_trend
        trend = {"bias": 0}
        for i in range(1, n):
            if closes[i] > (high_piv.current or -1e18) and not high_piv.crossed:
                tag = "CHoCH" if trend["bias"] == BEAR else "BOS"
                high_piv.crossed = True
                trend["bias"] = BULL
                if internal:
                    internal_trend = BULL
                    res.internal_trend = BULL
                else:
                    swing_trend = BULL
                    res.swing_trend = BULL
                push_event(("internal_" if internal else "swing_") + ("choch" if tag == "CHoCH" else "bos"),
                           i, direction="bull", tag=tag, level=high_piv.current)
                drawings["lines"].append({
                    "x1": high_piv.bar_index, "y1": high_piv.current or 0,
                    "x2": i, "y2": high_piv.current or 0,
                    "style": "dashed" if internal else "solid",
                    "color": "green", "tag": tag,
                })
                drawings["labels"].append({"x": i, "y": high_piv.current, "text": tag, "side": "down"})
                if show.get("obs"):
                    # bullish OB at last opposite extreme before break
                    store_ob(low_piv, internal, BULL, i)
            if closes[i] < (low_piv.current or 1e18) and not low_piv.crossed:
                tag = "CHoCH" if trend["bias"] == BULL else "BOS"
                low_piv.crossed = True
                trend["bias"] = BEAR
                if internal:
                    internal_trend = BEAR
                    res.internal_trend = BEAR
                else:
                    swing_trend = BEAR
                    res.swing_trend = BEAR
                push_event(("internal_" if internal else "swing_") + ("choch" if tag == "CHoCH" else "bos"),
                           i, direction="bear", tag=tag, level=low_piv.current)
                drawings["lines"].append({
                    "x1": low_piv.bar_index, "y1": low_piv.current or 0,
                    "x2": i, "y2": low_piv.current or 0,
                    "style": "dashed" if internal else "solid",
                    "color": "red", "tag": tag,
                })
                drawings["labels"].append({"x": i, "y": low_piv.current, "text": tag, "side": "up"})
                if show.get("obs"):
                    store_ob(high_piv, internal, BEAR, i)

    def store_ob(piv: Pivot, internal: bool, bias: int, end_i: int):
        # find extreme opposite leg from pivot bar to end
        s = max(0, piv.bar_index)
        e = min(n, end_i + 1)
        if e <= s:
            return
        if bias == BEAR:
            k = max(range(s, e), key=lambda j: parsed_high[j])
            oh, ol, ot = parsed_high[k], parsed_low[k], times[k]
        else:
            k = min(range(s, e), key=lambda j: parsed_low[j])
            oh, ol, ot = parsed_high[k], parsed_low[k], times[k]
        ob = OrderBlock(bar_high=oh, bar_low=ol, bar_time=ot, bias=bias, bar_index=k)
        target = res.internal_order_blocks if internal else res.swing_order_blocks
        target.insert(0, ob)
        drawings["boxes"].append({
            "type": "order_block", "internal": internal, "bias": "bull" if bias == BULL else "bear",
            "x1": ot, "y1": oh, "x2": times[-1], "y2": ol,
        })

    if show.get("swings") or show.get("obs"):
        display_structure(swing_low, swing_high, internal=False)
    if show.get("internals") or show.get("obs"):
        display_structure(internal_low, internal_high, internal=True)

    # OB mitigation
    def mitigate(internal: bool):
        arr = res.internal_order_blocks if internal else res.swing_order_blocks
        keep = []
        for ob in arr:
            src_hi = highs[-1] if True else 0
            src_lo = lows[-1]
            if ob.bias == BEAR and src_hi > ob.bar_high:
                push_event("ob_mitigated", n - 1, bias="bear", internal=internal)
                continue
            if ob.bias == BULL and src_lo < ob.bar_low:
                push_event("ob_mitigated", n - 1, bias="bull", internal=internal)
                continue
            keep.append(ob)
        if internal:
            res.internal_order_blocks = keep
        else:
            res.swing_order_blocks = keep

    mitigate(True)
    mitigate(False)

    # FVG 3-bar
    if show.get("fvg"):
        for i in range(2, n):
            if lows[i] > highs[i - 2] and closes[i - 1] > highs[i - 2]:
                f = FVG(top=lows[i], bottom=highs[i - 2], bias=BULL, left_time=times[i - 2], right_time=times[i])
                res.fvgs.append(f)
                drawings["boxes"].append({"type": "fvg", "bias": "bull", "x1": f.left_time, "y1": f.top, "x2": f.right_time, "y2": f.bottom})
                push_event("bullish_fvg", i, top=f.top, bottom=f.bottom)
            if highs[i] < lows[i - 2] and closes[i - 1] < lows[i - 2]:
                f = FVG(top=lows[i - 2], bottom=highs[i], bias=BEAR, left_time=times[i - 2], right_time=times[i])
                res.fvgs.append(f)
                drawings["boxes"].append({"type": "fvg", "bias": "bear", "x1": f.left_time, "y1": f.top, "x2": f.right_time, "y2": f.bottom})
                push_event("bearish_fvg", i, top=f.top, bottom=f.bottom)

    # trailing extremes + premium/discount
    top = max(highs)
    bottom = min(lows)
    res.trailing = {"top": top, "bottom": bottom, "top_time": times[highs.index(top)],
                    "bottom_time": times[lows.index(bottom)]}
    eq = (top + bottom) / 2
    res.premium_discount = {
        "premium_top": top,
        "premium_bottom": 0.95 * top + 0.05 * bottom,
        "equilibrium": eq,
        "eq_band_top": 0.525 * top + 0.475 * bottom,
        "eq_band_bottom": 0.525 * bottom + 0.475 * top,
        "discount_top": 0.95 * bottom + 0.05 * top,
        "discount_bottom": bottom,
        "current_zone": "premium" if closes[-1] > eq else "discount",
    }
    if show.get("zones"):
        drawings["boxes"].append({"type": "premium", "x1": 0, "y1": top, "x2": n - 1, "y2": 0.95 * top + 0.05 * bottom})
        drawings["boxes"].append({"type": "discount", "x1": 0, "y1": 0.95 * bottom + 0.05 * top, "x2": n - 1, "y2": bottom})

    if show.get("hl"):
        strong_high = trailing_top == top
        drawings["labels"].append({"x": n - 1, "y": top, "text": "Strong High" if swing_trend == BEAR else "Weak High", "side": "down"})
        drawings["labels"].append({"x": n - 1, "y": bottom, "text": "Strong Low" if swing_trend == BULL else "Weak Low", "side": "up"})

    res.events = events
    res.drawings = drawings
    return res


def smc_summary(res: SMCResult) -> dict:
    """Compact JSON for LLM tools."""
    return {
        "swing_trend": {1: "bull", -1: "bear", 0: "neutral"}.get(res.swing_trend, "neutral"),
        "internal_trend": {1: "bull", -1: "bear", 0: "neutral"}.get(res.internal_trend, "neutral"),
        "events": res.events[-12:],
        "order_blocks": {
            "swing": [{"bias": "bull" if o.bias == BULL else "bear", "high": o.bar_high, "low": o.bar_low} for o in res.swing_order_blocks[:5]],
            "internal": [{"bias": "bull" if o.bias == BULL else "bear", "high": o.bar_high, "low": o.bar_low} for o in res.internal_order_blocks[:5]],
        },
        "equal_highs": res.equal_highs[-3:],
        "equal_lows": res.equal_lows[-3:],
        "fvgs": [{"bias": "bull" if f.bias == BULL else "bear", "top": f.top, "bottom": f.bottom} for f in res.fvgs[-5:]],
        "premium_discount": res.premium_discount,
        "trailing": res.trailing,
    }
