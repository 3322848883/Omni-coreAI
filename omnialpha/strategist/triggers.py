"""Configurable event triggers for plan-loop (per-bot strategy).

Condition dict shapes (all optional fields documented in README):

  {"type": "kline_close"}                                  # handled by loop
  {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 20, "side": "above"|"below"}
  {"type": "ema_cross", "symbol": "BTC_USDT", "fast": 9, "slow": 21, "dir": "up"|"down"|"any"}
  {"type": "atr_spike", "symbol": "BTC_USDT", "period": 14, "mult": 1.5, "lookback": 20}
  {"type": "price_break", "symbol": "BTC_USDT", "lookback": 20, "side": "high"|"low"}
  {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "gt"|"lt", "level": 70}
  {"type": "ma_cross", "symbol": "BTC_USDT", "fast": 7, "slow": 30, "dir": "up"|"down"|"any", "ma": "sma"|"ema"}
  {"type": "macd_cross", "symbol": "BTC_USDT", "fast": 12, "slow": 26, "signal": 9, "dir": "up"|"down"|"any"}
  {"type": "boll_break", "symbol": "BTC_USDT", "period": 20, "k": 2, "side": "upper"|"lower"}
  {"type": "volume_spike", "symbol": "BTC_USDT", "mult": 2, "lookback": 20}

Each condition fires at most once per `cooldown_sec` (default 60) after it last fired.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .indicators import atr, boll, ema, macd, rsi, sma

log = logging.getLogger("omnialpha.triggers")

LEAF_CONDITIONS = frozenset({
    "price_vs_ema", "ema_cross", "atr_spike", "price_break", "rsi",
    "ma_cross", "macd_cross", "boll_break", "volume_spike",
})
COMBINATORS = frozenset({"all", "any"})
KNOWN_CONDITIONS = LEAF_CONDITIONS | COMBINATORS
MAX_COND_DEPTH = 2


class ConditionError(ValueError):
    pass


@dataclass
class ConditionState:
    last_fire: float = 0.0
    last_values: dict = field(default_factory=dict)


def parse_conditions(raw: Optional[list], _depth: int = 0) -> list[dict]:
    """Parse and validate conditions. Unknown types raise ConditionError."""
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            raise ConditionError("condition must be an object")
        ctype = str(item.get("type") or "").strip().lower()
        if ctype in ("", "kline_close"):
            continue
        if ctype not in KNOWN_CONDITIONS:
            raise ConditionError(
                f"unknown condition type {ctype!r}; supported: "
                f"{','.join(sorted(KNOWN_CONDITIONS))}"
            )
        item = dict(item)
        item["type"] = ctype
        item.setdefault("cooldown_sec", 60)
        if ctype in COMBINATORS:
            if _depth >= MAX_COND_DEPTH:
                raise ConditionError(f"condition nesting deeper than {MAX_COND_DEPTH}")
            children = item.get("children")
            if not isinstance(children, list) or not children:
                raise ConditionError(f"{ctype} requires non-empty children[]")
            item["children"] = parse_conditions(children, _depth=_depth + 1)
        else:
            item.setdefault("symbol", item.get("symbol") or "")
        out.append(item)
    return out


def _load_candles(client, symbol: str, interval: str, limit: int) -> list[dict]:
    """取 K 线（升序）。

    必须走 fetch_rest_candles：它对新旧客户端都兼容（有 get_klines 用 get_klines，
    否则回退 public_get）。此前这里直接调 client.public_get，而客户端重构成
    ExchangeClient 后**没有该方法** → 每次条件求值都抛 AttributeError、被上层
    兜底成 False，导致 AI 自设触发器与条件事件**永远不会触发**（线上实测：
    178 个周期全部是 interval，cond[...] 出现 0 次）。
    """
    from .market import fetch_rest_candles

    rows = fetch_rest_candles(client, symbol, interval, limit)
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        out.append({
            "t": int(r.get("t") or 0),
            "v": float(r.get("v") or 0),
            "c": float(r.get("c") or 0),
            "h": float(r.get("h") or 0),
            "l": float(r.get("l") or 0),
            "o": float(r.get("o") or 0),
        })
    out.sort(key=lambda x: x["t"])
    return out


def evaluate_condition(client, cond: dict, timeframe: str, now: Optional[float] = None) -> tuple[bool, str]:
    """Return (fired, human reason)."""
    ts = now if now is not None else time.time()
    ctype = cond.get("type")

    if ctype in COMBINATORS:
        children = cond.get("children") or []
        if not children:
            return False, f"{ctype} empty"
        parts = []
        oks = []
        for ch in children:
            ok, reason = evaluate_condition(client, ch, timeframe, now=ts)
            oks.append(ok)
            parts.append(f"{ch.get('type')}:{'Y' if ok else 'N'}({reason[:40]})")
        joined = "; ".join(parts)
        if ctype == "all":
            return all(oks), f"all[{joined}]"
        return any(oks), f"any[{joined}]"

    symbol = cond.get("symbol") or ""
    if not symbol:
        return False, "no symbol"
    # ensure enough history for the longest indicator period
    period_hint = max(
        int(cond.get("period") or 20),
        int(cond.get("fast") or 9),
        int(cond.get("slow") or 21),
        int(cond.get("lookback") or 20) + 5,
    )
    limit = int(cond.get("candles") or max(80, period_hint + 30))
    try:
        rows = _load_candles(client, symbol, timeframe, max(limit, 30))
    except Exception as e:  # noqa: BLE001
        return False, f"candles error: {e}"
    if len(rows) < 5:
        return False, "not enough candles"

    closes = [r["c"] for r in rows]
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]
    last = closes[-1]

    if ctype == "price_vs_ema":
        period = int(cond.get("period") or 20)
        side = str(cond.get("side") or "above").lower()
        series = ema(closes, period)
        e = series[-1]
        if e is None:
            return False, "ema not ready"
        if side == "above":
            return last > e, f"last={last:.4f} ema{period}={e:.4f}"
        return last < e, f"last={last:.4f} ema{period}={e:.4f}"

    if ctype == "ema_cross":
        fast_n = int(cond.get("fast") or 9)
        slow_n = int(cond.get("slow") or 21)
        direction = str(cond.get("dir") or "any").lower()
        if len(closes) < slow_n + 2:
            return False, "not enough for ema_cross"
        f_s = ema(closes, fast_n)
        s_s = ema(closes, slow_n)
        if None in (f_s[-1], f_s[-2], s_s[-1], s_s[-2]):
            return False, "ema not ready"
        now_up = f_s[-1] > s_s[-1]
        prev_up = f_s[-2] > s_s[-2]
        crossed_up = now_up and not prev_up
        crossed_down = (not now_up) and prev_up
        if direction == "up" and crossed_up:
            return True, f"ema{fast_n}>{slow_n} cross up"
        if direction == "down" and crossed_down:
            return True, f"ema{fast_n}<{slow_n} cross down"
        if direction == "any" and (crossed_up or crossed_down):
            return True, f"ema{fast_n}/{slow_n} cross {'up' if crossed_up else 'down'}"
        return False, f"fast={f_s[-1]:.4f} slow={s_s[-1]:.4f}"

    if ctype == "atr_spike":
        period = int(cond.get("period") or 14)
        mult = float(cond.get("mult") or 1.5)
        lookback = int(cond.get("lookback") or 20)
        series = atr(highs, lows, closes, period)
        if series[-1] is None:
            return False, "atr not ready"
        window = [x for x in series[-lookback - 1 : -1] if x is not None]
        if not window:
            return False, "atr window empty"
        avg = sum(window) / len(window)
        return series[-1] > avg * mult, f"atr={series[-1]:.4f} avg={avg:.4f} x{mult}"

    if ctype == "price_break":
        lookback = int(cond.get("lookback") or 20)
        side = str(cond.get("side") or "high").lower()
        window = closes[-lookback - 1 : -1]
        if len(window) < lookback:
            return False, "not enough lookback"
        if side == "high":
            hi = max(window)
            return last > hi, f"last={last:.4f} hi{lookback}={hi:.4f}"
        lo = min(window)
        return last < lo, f"last={last:.4f} lo{lookback}={lo:.4f}"

    if ctype == "rsi":
        period = int(cond.get("period") or 14)
        op = str(cond.get("op") or "gt").lower()
        level = float(cond.get("level") or 70)
        series = rsi(closes, period)
        val = series[-1]
        if val is None:
            return False, "rsi not ready"
        if op == "gt":
            return val > level, f"rsi{period}={val:.2f} > {level}"
        return val < level, f"rsi{period}={val:.2f} < {level}"

    if ctype == "ma_cross":
        fast_n = int(cond.get("fast") or 7)
        slow_n = int(cond.get("slow") or 30)
        direction = str(cond.get("dir") or "any").lower()
        kind = str(cond.get("ma") or "sma").lower()  # sma | ema
        if len(closes) < slow_n + 2:
            return False, "not enough for ma_cross"
        f_s = (ema if kind == "ema" else sma)(closes, fast_n)
        s_s = (ema if kind == "ema" else sma)(closes, slow_n)
        if None in (f_s[-1], f_s[-2], s_s[-1], s_s[-2]):
            return False, "ma not ready"
        now_up = f_s[-1] > s_s[-1]
        prev_up = f_s[-2] > s_s[-2]
        crossed_up = now_up and not prev_up
        crossed_down = (not now_up) and prev_up
        if direction == "up" and crossed_up:
            return True, f"ma{fast_n}>{slow_n} cross up"
        if direction == "down" and crossed_down:
            return True, f"ma{fast_n}<{slow_n} cross down"
        if direction == "any" and (crossed_up or crossed_down):
            return True, f"ma{fast_n}/{slow_n} cross {'up' if crossed_up else 'down'}"
        return False, f"fast={f_s[-1]:.4f} slow={s_s[-1]:.4f}"

    if ctype == "macd_cross":
        fast_n = int(cond.get("fast") or 12)
        slow_n = int(cond.get("slow") or 26)
        sig_n = int(cond.get("signal") or 9)
        direction = str(cond.get("dir") or "any").lower()
        series = macd(closes, fast_n, slow_n, sig_n)
        dif, dea = series["dif"], series["dea"]
        if None in (dif[-1], dif[-2], dea[-1], dea[-2]):
            return False, "macd not ready"
        now_up = dif[-1] > dea[-1]
        prev_up = dif[-2] > dea[-2]
        crossed_up = now_up and not prev_up
        crossed_down = (not now_up) and prev_up
        if direction == "up" and crossed_up:
            return True, f"macd cross up dif={dif[-1]:.4f} dea={dea[-1]:.4f}"
        if direction == "down" and crossed_down:
            return True, f"macd cross down dif={dif[-1]:.4f} dea={dea[-1]:.4f}"
        if direction == "any" and (crossed_up or crossed_down):
            return True, f"macd cross {'up' if crossed_up else 'down'}"
        return False, f"dif={dif[-1]:.4f} dea={dea[-1]:.4f}"

    if ctype == "boll_break":
        period = int(cond.get("period") or 20)
        kb = float(cond.get("k") or 2.0)
        side = str(cond.get("side") or "upper").lower()  # upper | lower
        series = boll(closes, period, kb)
        up, lo = series["upper"][-1], series["lower"][-1]
        if up is None or lo is None:
            return False, "boll not ready"
        if side == "upper":
            return last > up, f"last={last:.4f} boll_upper={up:.4f}"
        return last < lo, f"last={last:.4f} boll_lower={lo:.4f}"

    if ctype == "volume_spike":
        mult = float(cond.get("mult") or 2.0)
        lookback = int(cond.get("lookback") or 20)
        vols = [r["v"] for r in rows[-lookback - 1 : -1]]
        if not vols:
            return False, "volume window empty"
        avg = sum(vols) / len(vols)
        cur = rows[-1]["v"]
        return cur > avg * mult, f"vol={cur:.0f} avg={avg:.0f} x{mult}"

    return False, f"unknown condition {ctype}"


def check_conditions(
    client,
    conditions: list[dict],
    timeframe: str,
    states: Optional[dict] = None,
    now: Optional[float] = None,
) -> list[dict]:
    """Evaluate all conditions; return [{type, symbol, reason}] for newly fired ones."""
    states = states if states is not None else {}
    ts = now if now is not None else time.time()
    fired = []
    for i, cond in enumerate(conditions):
        key = cond.get("key") or f"{cond.get('type')}:{cond.get('symbol')}:{i}"
        st: ConditionState = states.setdefault(key, ConditionState())
        cooldown = float(cond.get("cooldown_sec") or 60)
        if ts - st.last_fire < cooldown:
            continue
        ok, reason = evaluate_condition(client, cond, timeframe, now=ts)
        if ok:
            st.last_fire = ts
            fired.append({
                "type": cond.get("type"),
                "symbol": cond.get("symbol") or (cond.get("children") or [{}])[0].get("symbol"),
                "reason": reason,
                "key": key,
            })
    return fired
