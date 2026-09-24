"""Local technical indicators (EMA / RSI / ATR) for strategist snapshots.

Main path may keep pa-data-source ema20/atr14 when present; other fields are
always computed here. Formulas match pa-data-source kline_watcher (Wilder).
"""
from __future__ import annotations

from typing import Any, Optional


def ema(closes: list[float], period: int) -> list[Optional[float]]:
    """EMA seeded with SMA at index period-1; earlier slots are None."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    sma = sum(closes[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder RSI; None until enough change bars (index >= period)."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period + 1:
        return out
    gains = []
    losses = []
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[Optional[float]]:
    """Wilder ATR; index 0 is None, first value at index `period`."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < 2 or period <= 0:
        return out
    trs: list[float] = []
    for i in range(1, n):
        pc = closes[i - 1]
        tr = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
        trs.append(tr)
        if i < period:
            out[i] = None
        elif i == period:
            out[i] = sum(trs) / period
        else:
            prev = out[i - 1]
            assert prev is not None
            out[i] = (prev * (period - 1) + tr) / period
    return out


def _series(rows: list[dict[str, Any]], key: str) -> list[float]:
    vals: list[float] = []
    for r in rows:
        v = r.get(key)
        vals.append(float(v) if v is not None else 0.0)
    return vals


def attach_indicators(rows: list[dict[str, Any]], wanted: list[str] | None = None) -> list[dict[str, Any]]:
    """Attach indicator columns to candle rows.

    Keeps non-null DB-provided ema20/atr14; computes missing ones and
    always computes ema50/rsi14 when requested.
    """
    if not rows:
        return rows
    wanted = list(wanted or ["ema20", "ema50", "atr14", "rsi14"])
    closes = _series(rows, "c")
    highs = _series(rows, "h")
    lows = _series(rows, "l")

    ema_map = {
        "ema20": lambda: ema(closes, 20),
        "ema50": lambda: ema(closes, 50),
    }
    atr_map = {"atr14": lambda: atr(highs, lows, closes, 14)}
    rsi_map = {"rsi14": lambda: rsi(closes, 14)}

    computed: dict[str, list[Optional[float]]] = {}
    for name in wanted:
        if name in ema_map:
            computed[name] = ema_map[name]()
        elif name in atr_map:
            computed[name] = atr_map[name]()
        elif name in rsi_map:
            computed[name] = rsi_map[name]()

    for i, row in enumerate(rows):
        for name in wanted:
            if name in ("ema20", "atr14") and row.get(name) is not None:
                continue
            series = computed.get(name)
            if series is None:
                continue
            row[name] = series[i]
    return rows


def latest_indicators(rows: list[dict[str, Any]], wanted: list[str] | None = None) -> dict[str, Any]:
    """Latest non-null indicator values from attached rows."""
    wanted = list(wanted or ["ema20", "ema50", "atr14", "rsi14"])
    out: dict[str, Any] = {}
    if not rows:
        return {k: None for k in wanted}
    last = rows[-1]
    for name in wanted:
        out[name] = last.get(name)
    return out
