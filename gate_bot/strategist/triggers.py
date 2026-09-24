"""Configurable event triggers for plan-loop (per-bot strategy).

Condition dict shapes (all optional fields documented in README):

  {"type": "kline_close"}                                  # handled by loop
  {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 20, "side": "above"|"below"}
  {"type": "ema_cross", "symbol": "BTC_USDT", "fast": 9, "slow": 21, "dir": "up"|"down"|"any"}
  {"type": "atr_spike", "symbol": "BTC_USDT", "period": 14, "mult": 1.5, "lookback": 20}
  {"type": "price_break", "symbol": "BTC_USDT", "lookback": 20, "side": "high"|"low"}
  {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "gt"|"lt", "level": 70}

Each condition fires at most once per `cooldown_sec` (default 60) after it last fired.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .indicators import atr, ema, rsi

log = logging.getLogger("gate_bot.triggers")


@dataclass
class ConditionState:
    last_fire: float = 0.0
    last_values: dict = field(default_factory=dict)


def parse_conditions(raw: Optional[list]) -> list[dict]:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("type") or "").strip().lower()
        if ctype in ("", "kline_close"):
            continue  # kline_close is a first-class flag, not a list condition
        item = dict(item)
        item["type"] = ctype
        item.setdefault("symbol", item.get("symbol") or "")
        item.setdefault("cooldown_sec", 60)
        out.append(item)
    return out


def _load_candles(client, symbol: str, interval: str, limit: int) -> list[dict]:
    raw = client.public_get(
        "/api/v4/futures/usdt/candlesticks",
        f"contract={symbol}&interval={interval}&limit={limit}",
    )
    rows = []
    for r in raw or []:
        if isinstance(r, (list, tuple)) and len(r) >= 6:
            rows.append({
                "t": int(r[0]), "v": float(r[1]), "c": float(r[2]),
                "h": float(r[3]), "l": float(r[4]), "o": float(r[5]),
            })
        elif isinstance(r, dict):
            rows.append({
                "t": int(r.get("t") or 0),
                "o": float(r.get("o") or 0), "h": float(r.get("h") or 0),
                "l": float(r.get("l") or 0), "c": float(r.get("c") or 0),
                "v": float(r.get("v") or 0),
            })
    rows.sort(key=lambda x: x["t"])
    return rows


def evaluate_condition(client, cond: dict, timeframe: str, now: Optional[float] = None) -> tuple[bool, str]:
    """Return (fired, human reason)."""
    ts = now if now is not None else time.time()
    ctype = cond.get("type")
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
                "symbol": cond.get("symbol"),
                "reason": reason,
                "key": key,
            })
    return fired
