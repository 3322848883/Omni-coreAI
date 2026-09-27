"""SMC 结构事件（smc_events）— 与 smc_map 并存的另一套。

**作用**：回答「刚刚发生了什么 / 何时进场」——事件流 + 有效性标记。
  1. 市场结构：pivothigh/pivotlow 枢轴 → CHoCH / BOS + 扫荡(x)
  2. 订单块 OB：ATR Length 构造 + Close/Wick/Avg 缓解 + Breaker + 重叠过滤
  3. 公允价值缺口 FVG：阈值过滤 + 缓解/Breaker + 突袭(raid)

与 smc_map（市场地图：方向/估值区/关键位）互补：
  smc_map  → 在哪、往哪
  smc_events → 发生了什么、何时动手

rows: [{t,o,h,l,c,v}, ...]  chronological，t 为 unix 秒。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

BULL = 1
BEAR = -1


# ── 数据结构 ────────────────────────────────────────────────────────
@dataclass
class OB:
    bull: bool
    top: float
    btm: float
    avg: float
    bar_time: int
    bar_index: int
    volume: float = 0.0
    is_breaker: bool = False
    breaker_time: Optional[int] = None
    breaker_bar: Optional[int] = None
    removed: bool = False
    # 买卖活动
    dir: int = 1
    move: int = 1
    bl_pos: int = 1
    br_pos: int = 1
    vol_share: float = 0.0


@dataclass
class FVG:
    top: float
    btm: float
    bull: bool
    bar_time: int
    bar_index: int
    is_breaker: bool = False
    breaker_time: Optional[int] = None
    breaker_bar: Optional[int] = None
    removed: bool = False
    is_raid: bool = False
    raid_time: Optional[int] = None
    raid_price: Optional[float] = None
    raid_active: bool = False


@dataclass
class StructureEvent:
    kind: str          # "bos" | "choch"
    direction: str     # "bull" | "bear"
    level: float
    bar_index: int
    bar_time: int
    sweep: bool = False


@dataclass
class SMCEventsResult:
    trend: int = 0
    last_event: Optional[str] = None
    events: list = field(default_factory=list)
    sweeps: list = field(default_factory=list)
    bull_obs: list = field(default_factory=list)
    bear_obs: list = field(default_factory=list)
    bull_fvgs: list = field(default_factory=list)
    bear_fvgs: list = field(default_factory=list)
    structure_level: Optional[float] = None
    premium_discount: dict = field(default_factory=dict)
    drawings: dict = field(default_factory=dict)


# ── 基础工具 ────────────────────────────────────────────────────────
def _tr(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def true_range(highs, lows, closes) -> list[float]:
    n = len(highs)
    out = []
    for i in range(n):
        if i == 0:
            out.append(highs[i] - lows[i])
        else:
            out.append(_tr(highs[i], lows[i], closes[i - 1]))
    return out


def rma(values: list[float], period: int) -> list[Optional[float]]:
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


def atr_series(highs, lows, closes, period: int = 200) -> list[Optional[float]]:
    return rma(true_range(highs, lows, closes), period)


def pivothigh(highs: list[float], left: int, right: int) -> list[Optional[float]]:
    n = len(highs)
    out: list[Optional[float]] = [None] * n
    for i in range(left, n - right):
        h = highs[i]
        ok = all(highs[i - j] < h for j in range(1, left + 1)) and \
             all(highs[i + j] < h for j in range(1, right + 1))
        if ok:
            out[i + right] = h
    return out


def pivotlow(lows: list[float], left: int, right: int) -> list[Optional[float]]:
    n = len(lows)
    out: list[Optional[float]] = [None] * n
    for i in range(left, n - right):
        l = lows[i]
        ok = all(lows[i - j] > l for j in range(1, left + 1)) and \
             all(lows[i + j] > l for j in range(1, right + 1))
        if ok:
            out[i + right] = l
    return out


# ── 订单块 ─────────────────────────────────────────────────────────
def _ob_cords(highs, lows, atrs, bull: bool, idx: int, ob_mode: str = "length") -> float:
    if ob_mode == "full":
        return lows[idx] if bull else highs[idx]
    a = atrs[idx] if idx < len(atrs) and atrs[idx] is not None else 0.0
    if bull:
        cand = lows[idx] + a
        return highs[idx] if cand > highs[idx] else cand
    cand = highs[idx] - a
    return lows[idx] if cand < lows[idx] else cand


def _ob_action(ob: OB, o: float, h: float, l: float, c: float, method: str) -> str:
    if not ob.is_breaker:
        if ob.bull:
            hit = (min(c, o) < ob.btm) if method == "close" else \
                  (l < ob.btm) if method == "wick" else \
                  (l < ob.avg) if method == "avg" else False
        else:
            hit = (max(c, o) > ob.top) if method == "close" else \
                  (h > ob.top) if method == "wick" else \
                  (h > ob.avg) if method == "avg" else False
        return "to_breaker" if hit else "none"
    if ob.bull:
        hit = (max(c, o) > ob.top) if method == "close" else \
              (h > ob.top) if method == "wick" else \
              (h > ob.avg) if method == "avg" else False
    else:
        hit = (min(c, o) < ob.btm) if method == "close" else \
              (l < ob.btm) if method == "wick" else \
              (l < ob.avg) if method == "avg" else False
    return "remove" if hit else "none"


def _overlap(a_top: float, a_btm: float, b_top: float, b_btm: float) -> bool:
    return a_btm < b_top and a_top > b_btm


def _dedupe(items, top_key="top", btm_key="btm"):
    if len(items) < 2:
        return list(items)
    out = [items[0]]
    seen = {(
        getattr(items[0], "bar_time", None),
        round(getattr(items[0], top_key), 8),
        round(getattr(items[0], btm_key), 8),
    )}
    for x in items[1:]:
        ident = (
            getattr(x, "bar_time", None),
            round(getattr(x, top_key), 8),
            round(getattr(x, btm_key), 8),
        )
        if ident in seen:
            continue
        skip = False
        for kept in out:
            if _overlap(getattr(x, top_key), getattr(x, btm_key),
                        getattr(kept, top_key), getattr(kept, btm_key)):
                skip = True
                break
        if not skip:
            out.append(x)
            seen.add(ident)
    return out


# ── FVG ────────────────────────────────────────────────────────────
def _fvg_action(f: FVG, o: float, h: float, l: float, c: float, method: str) -> str:
    mid = (f.top + f.btm) / 2
    if not f.is_breaker:
        if f.bull:
            hit = (min(c, o) < f.btm) if method == "close" else \
                  (l < f.btm) if method == "wick" else \
                  (l < mid) if method == "avg" else False
        else:
            hit = (max(c, o) > f.top) if method == "close" else \
                  (h > f.top) if method == "wick" else \
                  (h > mid) if method == "avg" else False
        return "to_breaker" if hit else "none"
    if f.bull:
        hit = (max(c, o) > f.top) if method == "close" else \
              (h > f.top) if method == "wick" else \
              (h > mid) if method == "avg" else False
    else:
        hit = (min(c, o) < f.btm) if method == "close" else \
              (l < f.btm) if method == "wick" else \
              (l < mid) if method == "avg" else False
    return "remove" if hit else "none"


# ── 主计算 ─────────────────────────────────────────────────────────
def compute_smc_events(
    rows: list[dict],
    *,
    ms_len: int = 5,
    ob_mode: str = "length",
    ob_mitigate: str = "close",
    fvg_mitigate: str = "close",
    fvg_thresh: float = 0.0,
    atr_period: int = 200,
    ob_atr_len: int = 5,
    hide_ob_overlap: bool = True,
    hide_fvg_overlap: bool = True,
    ob_last: int = 10,
    fvg_last: int = 10,
    buildsweep: bool = True,
    features: Optional[dict] = None,
) -> SMCEventsResult:
    """SMC 结构事件流（smc_events）。

    features 默认全部打开：
      sweeps / breakers / raid / activity / metric / midline / bubble / trend_color
    """
    feat = {
        "sweeps": True, "breakers": True, "raid": True, "activity": True,
        "metric": True, "midline": True, "bubble": True, "trend_color": True,
    }
    if features:
        feat.update({k: bool(v) for k, v in features.items()})
    n = len(rows)
    res = SMCEventsResult()
    res.drawings = {"labels": [], "lines": [], "boxes": []}
    if n < 5:
        return res

    highs = [float(r["h"]) for r in rows]
    lows = [float(r["l"]) for r in rows]
    opens = [float(r.get("o") or r["c"]) for r in rows]
    closes = [float(r["c"]) for r in rows]
    vols = [float(r.get("v") or 0) for r in rows]
    times = [int(r.get("t") or i) for i, r in enumerate(rows)]

    atrs = atr_series(highs, lows, closes, atr_period)

    ph = pivothigh(highs, ms_len, ms_len)
    pl = pivotlow(lows, ms_len, ms_len)

    php: list[float] = []
    phn: list[int] = []
    plp: list[float] = []
    pln: list[int] = []

    start = 0
    trend = 0
    bos: Optional[float] = None
    choch: Optional[float] = None
    main = 0.0
    loc = 0
    temp = 0
    up = highs[0]
    dn = lows[0]
    last_txt: Optional[str] = None

    bull_obs: list[OB] = []
    bear_obs: list[OB] = []
    bull_fvgs: list[FVG] = []
    bear_fvgs: list[FVG] = []

    def push_event(kind, direction, level, i, sweep=False):
        res.events.append(StructureEvent(
            kind=kind, direction=direction, level=level,
            bar_index=i, bar_time=times[i], sweep=sweep,
        ))
        if sweep:
            res.sweeps.append({"bar": i, "time": times[i], "direction": direction,
                               "level": level, "kind": kind})

    def make_ob(bull: bool, cords: float, idx: int) -> OB:
        if bull:
            return OB(True, cords, lows[idx], (cords + lows[idx]) / 2,
                      times[idx], idx, vols[idx],
                      dir=(1 if closes[idx] > opens[idx] else -1))
        return OB(False, highs[idx], cords, (cords + highs[idx]) / 2,
                  times[idx], idx, vols[idx],
                  dir=(1 if closes[idx] > opens[idx] else -1))

    def find_extreme(use_max: bool, from_idx: int) -> int:
        if from_idx >= n - 1:
            return n - 1
        rng = range(max(0, from_idx), n)
        return max(rng, key=lambda j: highs[j]) if use_max else min(rng, key=lambda j: lows[j])

    for i in range(1, n):
        if ph[i] is not None:
            php.insert(0, ph[i])
            phn.insert(0, i - ms_len)
        if pl[i] is not None:
            plp.insert(0, pl[i])
            pln.insert(0, i - ms_len)
        if php and highs[i] > php[0]:
            php.clear()
            phn.clear()
        if plp and lows[i] < plp[0]:
            plp.clear()
            pln.clear()

        crossup = crossdn = False
        if highs[i] > up:
            up, dn = highs[i], lows[i]
            crossup = True
        if lows[i] < dn:
            up, dn = highs[i], lows[i]
            crossdn = True

        upsweep = dnsweep = False

        if start == 0:
            start = 1
            bos = highs[i]
            choch = lows[i]
            loc = temp = i
            main = highs[i]
            continue

        if start == 1:
            if buildsweep and lows[i] <= choch and closes[i] >= choch:
                dnsweep = True
                choch = lows[i]
                push_event("choch", "bear", choch, i, sweep=True)
            if buildsweep and highs[i] >= bos and closes[i] <= bos:
                upsweep = True
                bos = highs[i]
                push_event("choch", "bull", bos, i, sweep=True)
            if closes[i] <= choch:
                idbull = find_extreme(True, loc)
                bull_obs.insert(0, make_ob(True, _ob_cords(highs, lows, atrs, True, idbull, ob_mode), idbull))
                trend = BEAR
                old_bos = bos
                bos = None
                choch = old_bos if old_bos is not None else highs[i]
                start = 2
                loc = temp = i
                main = lows[i]
                last_txt = "choch"
                push_event("choch", "bear", closes[i], i)
                continue
            if closes[i] >= bos:
                idbear = find_extreme(True, loc)
                bear_obs.insert(0, make_ob(False, _ob_cords(highs, lows, atrs, False, idbear, ob_mode), idbear))
                trend = BULL
                old_choch = choch
                bos = None
                choch = old_choch if old_choch is not None else lows[i]
                start = 2
                loc = temp = i
                main = highs[i]
                last_txt = "choch"
                push_event("choch", "bull", closes[i], i)
                continue

        if start == 2:
            if trend == BEAR:
                if lows[i] <= main:
                    main, temp = lows[i], i
                if bos is not None and php and php[0] < choch:
                    choch, loc, temp = php[0], phn[0], phn[0]
                if bos is None:
                    if crossup and closes[i] > opens[i] and closes[i - 1] > opens[i - 1]:
                        bos, loc = main, temp
                        push_event("bos", "bear", bos, i)
                if bos is not None and buildsweep and lows[i] <= bos and closes[i] >= bos:
                    dnsweep = True
                    bos = lows[i]
                    push_event("bos", "bear", bos, i, sweep=True)
                elif bos is not None and closes[i] <= bos:
                    last_txt = "bos"
                    push_event("bos", "bear", bos, i)
                    idbear = find_extreme(True, loc)
                    bear_obs.insert(0, make_ob(False, _ob_cords(highs, lows, atrs, False, idbear, ob_mode), idbear))
                    bos = None
                    idh = find_extreme(True, loc if loc else i)
                    choch, loc = highs[idh], idh
                if choch is not None and buildsweep and highs[i] >= choch and closes[i] <= choch:
                    upsweep = True
                    choch = highs[i]
                    push_event("choch", "bull", choch, i, sweep=True)
                elif choch is not None and closes[i] >= choch:
                    last_txt = "choch"
                    push_event("choch", "bull", choch, i)
                    idbull = find_extreme(True, loc)
                    bull_obs.insert(0, make_ob(True, _ob_cords(highs, lows, atrs, True, idbull, ob_mode), idbull))
                    trend = BULL
                    bos = None
                    main = highs[i]
                    loc = temp = i
            else:
                if highs[i] >= main:
                    main, temp = highs[i], i
                if bos is not None and plp and plp[0] > choch:
                    choch, loc, temp = plp[0], pln[0], pln[0]
                if bos is None:
                    if crossdn and closes[i] < opens[i] and closes[i - 1] < opens[i - 1]:
                        bos, loc = main, temp
                        push_event("bos", "bull", bos, i)
                if bos is not None and buildsweep and highs[i] >= bos and closes[i] <= bos:
                    upsweep = True
                    bos = highs[i]
                    push_event("bos", "bull", bos, i, sweep=True)
                elif bos is not None and closes[i] >= bos:
                    last_txt = "bos"
                    push_event("bos", "bull", bos, i)
                    idbull = find_extreme(True, loc)
                    bull_obs.insert(0, make_ob(True, _ob_cords(highs, lows, atrs, True, idbull, ob_mode), idbull))
                    bos = None
                    idl = find_extreme(False, loc if loc else i)
                    choch, loc = lows[idl], idl
                if choch is not None and buildsweep and lows[i] <= choch and closes[i] >= choch:
                    dnsweep = True
                    choch = lows[i]
                    push_event("choch", "bear", choch, i, sweep=True)
                elif choch is not None and closes[i] <= choch:
                    last_txt = "choch"
                    push_event("choch", "bear", choch, i)
                    idbear = find_extreme(True, loc)
                    bear_obs.insert(0, make_ob(False, _ob_cords(highs, lows, atrs, False, idbear, ob_mode), idbear))
                    trend = BEAR
                    bos = None
                    main = lows[i]
                    loc = temp = i

        # OB 缓解 + 活动
        for arr in (bull_obs, bear_obs):
            for ob in arr:
                if ob.removed:
                    continue
                act = _ob_action(ob, opens[i], highs[i], lows[i], closes[i], ob_mitigate)
                if act == "to_breaker":
                    ob.is_breaker = True
                    ob.breaker_time = times[i]
                    ob.breaker_bar = i
                elif act == "remove":
                    if feat["breakers"]:
                        ob.is_breaker = True
                        if ob.breaker_time is None:
                            ob.breaker_time = times[i]
                            ob.breaker_bar = i
                    else:
                        ob.removed = True
                if feat["activity"]:
                    if ob.dir == 1:
                        if ob.move == 1:
                            ob.bl_pos += 1; ob.move = 2
                        elif ob.move == 2:
                            ob.bl_pos += 1; ob.move = 3
                        else:
                            ob.br_pos += 1; ob.move = 1
                    else:
                        if ob.move == 1:
                            ob.br_pos += 1; ob.move = 2
                        elif ob.move == 2:
                            ob.br_pos += 1; ob.move = 3
                        else:
                            ob.bl_pos += 1; ob.move = 1
        bull_obs = [b for b in bull_obs if not b.removed]
        bear_obs = [b for b in bear_obs if not b.removed]

        # FVG（延迟 1 根确认）
        if i >= 3:
            if lows[i - 1] > highs[i - 3]:
                thr = (atrs[i - 2] or 0.0) * fvg_thresh
                if fvg_thresh <= 0 or closes[i - 2] > (lows[i - 2] + thr):
                    bull_fvgs.insert(0, FVG(top=lows[i - 1], btm=highs[i - 3], bull=True,
                                            bar_time=times[i - 3], bar_index=i - 3))
            if lows[i - 3] > highs[i - 1]:
                thr = (atrs[i - 2] or 0.0) * fvg_thresh
                if fvg_thresh <= 0 or closes[i - 2] < (highs[i - 2] - thr):
                    bear_fvgs.insert(0, FVG(top=lows[i - 3], btm=highs[i - 1], bull=False,
                                            bar_time=times[i - 3], bar_index=i - 3))

        for arr in (bull_fvgs, bear_fvgs):
            for f in arr:
                if f.removed:
                    continue
                act = _fvg_action(f, opens[i], highs[i], lows[i], closes[i], fvg_mitigate)
                if act == "to_breaker":
                    f.is_breaker = True
                    f.breaker_time = times[i]
                    f.breaker_bar = i
                elif act == "remove":
                    if feat["breakers"]:
                        f.is_breaker = True
                        if f.breaker_time is None:
                            f.breaker_time = times[i]
                            f.breaker_bar = i
                    else:
                        f.removed = True
                if feat["raid"] and not f.is_breaker:
                    if f.bull and not f.is_raid:
                        if lows[i] < f.top and closes[i] > f.top:
                            f.is_raid = True
                            f.raid_time = times[i]
                            f.raid_price = lows[i]
                            f.raid_active = False
                    elif (not f.bull) and not f.is_raid:
                        if highs[i] > f.btm and closes[i] < f.btm:
                            f.is_raid = True
                            f.raid_time = times[i]
                            f.raid_price = highs[i]
                            f.raid_active = False
                    elif f.is_raid and not f.raid_active:
                        if f.bull and lows[i] <= (f.raid_price or 0):
                            f.raid_active = True
                        elif (not f.bull) and highs[i] >= (f.raid_price or 0):
                            f.raid_active = True
        bull_fvgs = [f for f in bull_fvgs if not f.removed]
        bear_fvgs = [f for f in bear_fvgs if not f.removed]

    if hide_ob_overlap:
        bull_obs = _dedupe(bull_obs)
        bear_obs = _dedupe(bear_obs)
    if hide_fvg_overlap:
        bull_fvgs = _dedupe(bull_fvgs)
        bear_fvgs = _dedupe(bear_fvgs)

    res.trend = trend
    res.last_event = last_txt
    res.structure_level = choch if choch is not None else bos
    if feat["metric"]:
        for arr in (bull_obs, bear_obs):
            total = sum(b.volume for b in arr) or 1.0
            for b in arr:
                b.vol_share = b.volume / total
    res.bull_obs = bull_obs[:ob_last]
    res.bear_obs = bear_obs[:ob_last]
    res.bull_fvgs = bull_fvgs[:fvg_last]
    res.bear_fvgs = bear_fvgs[:fvg_last]

    top, bottom = max(highs), min(lows)
    eq = (top + bottom) / 2
    res.premium_discount = {
        "premium_top": top,
        "equilibrium": eq,
        "discount_bottom": bottom,
        "current_zone": "premium" if closes[-1] > eq else "discount",
    }

    for e in res.events:
        res.drawings["lines"].append({
            "x1": e.bar_index, "y1": e.level, "x2": n - 1, "y2": e.level,
            "style": "dotted" if e.sweep else ("dashed" if e.kind == "choch" else "solid"),
            "color": "green" if e.direction == "bull" else "red",
            "tag": "x" if e.sweep else e.kind.upper(),
        })
        res.drawings["labels"].append({
            "x": e.bar_index, "y": e.level,
            "text": "x" if e.sweep else e.kind.upper(),
            "side": "up" if e.direction == "bear" else "down",
        })
        if feat["bubble"]:
            res.drawings["labels"].append({
                "x": e.bar_index, "y": e.level, "text": "",
                "style": "circle",
                "color": "green" if e.direction == "bull" else "red",
            })
    for ob in res.bull_obs + res.bear_obs:
        res.drawings["boxes"].append({
            "type": "order_block", "bias": "bull" if ob.bull else "bear",
            "breaker": ob.is_breaker,
            "x1": ob.bar_time, "y1": ob.top, "x2": times[-1], "y2": ob.btm,
        })
        if feat["midline"]:
            res.drawings["lines"].append({
                "x1": ob.bar_time, "y1": ob.avg, "x2": times[-1], "y2": ob.avg,
                "style": "dashed", "tag": "ob_mid",
                "color": "green" if ob.bull else "red",
            })
        if feat["activity"]:
            res.drawings["boxes"].append({
                "type": "ob_activity", "bias": "bull" if ob.bull else "bear",
                "x1": ob.bar_time, "y1": ob.top, "x2": times[-1], "y2": ob.avg,
                "side": "buy", "count": ob.bl_pos,
            })
            res.drawings["boxes"].append({
                "type": "ob_activity", "bias": "bull" if ob.bull else "bear",
                "x1": ob.bar_time, "y1": ob.avg, "x2": times[-1], "y2": ob.btm,
                "side": "sell", "count": ob.br_pos,
            })
    for f in res.bull_fvgs + res.bear_fvgs:
        res.drawings["boxes"].append({
            "type": "fvg", "bias": "bull" if f.bull else "bear",
            "breaker": f.is_breaker,
            "x1": f.bar_time, "y1": f.top, "x2": times[-1], "y2": f.btm,
        })
        if feat["midline"]:
            res.drawings["lines"].append({
                "x1": f.bar_time, "y1": (f.top + f.btm) / 2,
                "x2": times[-1], "y2": (f.top + f.btm) / 2,
                "style": "dashed", "tag": "fvg_mid",
                "color": "green" if f.bull else "red",
            })
        if feat["raid"] and f.is_raid:
            res.drawings["labels"].append({
                "x": f.bar_index, "y": f.raid_price or (f.top + f.btm) / 2,
                "text": "x", "style": "raid",
                "side": "up" if f.bull else "down",
            })
    if feat["trend_color"]:
        res.drawings["trend_color"] = (
            "green" if res.trend == BULL else "red" if res.trend == BEAR else "gray"
        )
        res.drawings["last_event"] = res.last_event
    return res


def smc_events_summary(res: SMCEventsResult) -> dict:
    def _ob(o: OB) -> dict:
        return {"bias": "bull" if o.bull else "bear", "top": o.top, "btm": o.btm,
                "avg": o.avg, "breaker": o.is_breaker, "time": o.bar_time,
                "volume": o.volume, "dir": o.dir,
                "bl_pos": o.bl_pos, "br_pos": o.br_pos, "vol_share": o.vol_share}

    def _f(f: FVG) -> dict:
        return {"bias": "bull" if f.bull else "bear", "top": f.top, "btm": f.btm,
                "breaker": f.is_breaker, "time": f.bar_time,
                "raid": f.is_raid, "raid_price": f.raid_price,
                "raid_active": f.raid_active}

    return {
        "trend": {1: "bull", -1: "bear", 0: "neutral"}.get(res.trend, "neutral"),
        "last_event": res.last_event,
        "structure_level": res.structure_level,
        "events": [
            {"kind": e.kind, "direction": e.direction, "level": e.level,
             "bar": e.bar_index, "time": e.bar_time, "sweep": e.sweep}
            for e in res.events[-12:]
        ],
        "sweeps": res.sweeps[-6:],
        "order_blocks": {"bull": [_ob(o) for o in res.bull_obs[:5]],
                         "bear": [_ob(o) for o in res.bear_obs[:5]]},
        "fvgs": {"bull": [_f(x) for x in res.bull_fvgs[:5]],
                 "bear": [_f(x) for x in res.bear_fvgs[:5]]},
        "premium_discount": res.premium_discount,
    }
