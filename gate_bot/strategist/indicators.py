"""Local technical indicators (EMA / RSI / ATR) for strategist snapshots.

Main path keeps pa-data-source ema20/atr14 when present and warm-starts any
gap from the last known DB value (never recomputes a detached window).
Formulas match pa-data-source kline_watcher: EMA k=2/(n+1), ATR/RSI Wilder.
"""
from __future__ import annotations

from typing import Any, Optional


def ema(closes: list[float], period: int) -> list[Optional[float]]:
    """Standard EMA (k=2/(period+1)), SMA-seeded at index period-1."""
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
    gains: list[float] = []
    losses: list[float] = []
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


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fill_ema_from(
    rows: list[dict[str, Any]],
    key: str,
    period: int,
    close_key: str = "c",
) -> None:
    """Write `key` in-place; keep existing values and warm-start gaps from state."""
    k = 2.0 / (period + 1)
    state: Optional[float] = None
    closes: list[Optional[float]] = [_f(r.get(close_key)) for r in rows]
    n = len(rows)
    for i in range(n):
        existing = _f(rows[i].get(key))
        if existing is not None:
            rows[i][key] = existing
            state = existing
            continue
        c = closes[i]
        if c is None:
            rows[i][key] = None
            continue
        if state is None:
            if i >= period - 1:
                window = [closes[j] for j in range(i - period + 1, i + 1)]
                if any(x is None for x in window):
                    rows[i][key] = None
                else:
                    sma = sum(window) / period  # type: ignore[arg-type]
                    rows[i][key] = sma
                    state = sma
            else:
                rows[i][key] = None
        else:
            state = c * k + state * (1.0 - k)
            rows[i][key] = state


def _fill_atr_from(
    rows: list[dict[str, Any]],
    key: str,
    period: int,
) -> None:
    """Wilder ATR in-place; keep existing values and warm-start gaps from state."""
    state: Optional[float] = None
    n = len(rows)
    tr_window: list[float] = []
    prev_c: Optional[float] = None
    for i in range(n):
        existing = _f(rows[i].get(key))
        h, l, c = _f(rows[i].get("h")), _f(rows[i].get("l")), _f(rows[i].get("c"))
        if existing is not None:
            rows[i][key] = existing
            state = existing
            prev_c = c if c is not None else prev_c
            continue
        if h is None or l is None or c is None:
            rows[i][key] = None
            prev_c = c if c is not None else prev_c
            continue
        if i == 0 or prev_c is None:
            rows[i][key] = None
            prev_c = c
            continue
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        if state is None:
            tr_window.append(tr)
            if len(tr_window) < period:
                rows[i][key] = None
            else:
                state = sum(tr_window) / period
                rows[i][key] = state
        else:
            state = (state * (period - 1) + tr) / period
            rows[i][key] = state
        prev_c = c


def _fill_rsi_from(rows: list[dict[str, Any]], key: str, period: int) -> None:
    """Wilder RSI in-place over rows with numeric closes; gaps leave None."""
    closes = [_f(r.get("c")) for r in rows]
    n = len(rows)
    gains: list[float] = []
    losses: list[float] = []
    avg_gain: Optional[float] = None
    avg_loss: Optional[float] = None
    for i in range(n):
        if i == 0 or closes[i] is None or closes[i - 1] is None:
            rows[i][key] = None
            continue
        ch = (closes[i] or 0.0) - (closes[i - 1] or 0.0)
        g, ls = max(ch, 0.0), max(-ch, 0.0)
        gains.append(g)
        losses.append(ls)
        if avg_gain is None:
            if len(gains) < period:
                rows[i][key] = None
                continue
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            rows[i][key] = _rsi_value(avg_gain, avg_loss)
            continue
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + ls) / period
        rows[i][key] = _rsi_value(avg_gain, avg_loss)


def attach_indicators(rows: list[dict[str, Any]], wanted: list[str] | None = None) -> list[dict[str, Any]]:
    """Attach indicator columns to candle rows.

    Keeps non-null DB-provided ema20/atr14 and continues any gap from the last
    known value (warm-start). Always computes ema50/rsi14 when requested.
    """
    if not rows:
        return rows
    wanted = list(wanted or ["ema20", "ema50", "atr14", "rsi14"])
    for name in wanted:
        if name == "ema20":
            _fill_ema_from(rows, "ema20", 20)
        elif name == "ema50":
            _fill_ema_from(rows, "ema50", 50)
        elif name == "atr14":
            _fill_atr_from(rows, "atr14", 14)
        elif name == "rsi14":
            _fill_rsi_from(rows, "rsi14", 14)
    return rows


def latest_indicators(rows: list[dict[str, Any]], wanted: list[str] | None = None) -> dict[str, Any]:
    """Indicator values from the last row (may be None if that bar is still forming)."""
    wanted = list(wanted or ["ema20", "ema50", "atr14", "rsi14"])
    if not rows:
        return {k: None for k in wanted}
    last = rows[-1]
    return {name: last.get(name) for name in wanted}
