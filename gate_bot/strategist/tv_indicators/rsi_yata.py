"""TV Indicator 2: RSI Yata

Enhanced RSI with smoothing, MA overlay, Bollinger Bands on RSI,
RSI candles, OB/OS dots, RSI/MACD histograms.
"""
from __future__ import annotations
import math
from typing import Optional


def rsi_base(closes: list[float], length: int = 14) -> list[Optional[float]]:
    """Core RSI using RMA smoothing (matches TV ta.rma)."""
    n = len(closes)
    out = [None] * n
    if n < length + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # RMA (Wilder's smoothing)
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100 - 100 / (1 + rs)
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
    return out


def rsi_smoothed(rsi_values: list[Optional[float]], smooth_len: int = 3
                 ) -> list[Optional[float]]:
    """EMA-smoothed RSI."""
    vals = [v if v is not None else 0 for v in rsi_values]
    # Simple EMA
    k = 2 / (smooth_len + 1)
    out = [None] * len(vals)
    if vals:
        out[0] = vals[0]
        for i in range(1, len(vals)):
            out[i] = vals[i] * k + out[i - 1] * (1 - k)
    return out


def rsi_ma(rsi_values: list[Optional[float]], length: int = 21,
           ma_type: str = "SMA") -> list[Optional[float]]:
    """Moving average of RSI (for BB and crossover signals)."""
    vals = [v if v is not None else 0 for v in rsi_values]
    if ma_type == "SMA":
        out = [None] * len(vals)
        for i in range(length - 1, len(vals)):
            out[i] = sum(vals[i - length + 1:i + 1]) / length
        return out
    elif ma_type == "EMA":
        k = 2 / (length + 1)
        out = [None] * len(vals)
        if vals:
            out[0] = vals[0]
            for i in range(1, len(vals)):
                out[i] = vals[i] * k + out[i - 1] * (1 - k)
        return out
    # Default SMA
    out = [None] * len(vals)
    for i in range(length - 1, len(vals)):
        out[i] = sum(vals[i - length + 1:i + 1]) / length
    return out


def rsi_bollinger(rsi_values: list[Optional[float]], length: int = 21,
                  mult: float = 2.0) -> dict:
    """Bollinger Bands on RSI."""
    ma = rsi_ma(rsi_values, length, "SMA")
    n = len(rsi_values)
    upper = [None] * n
    lower = [None] * n
    for i in range(length - 1, n):
        if ma[i] is None:
            continue
        seg = [v or 0 for v in rsi_values[i - length + 1:i + 1]]
        m = ma[i]
        std = math.sqrt(sum((x - m) ** 2 for x in seg) / length)
        upper[i] = m + mult * std
        lower[i] = m - mult * std
    return {"ma": ma, "upper": upper, "lower": lower}


def rsi_candles(rsi_values: list[Optional[float]], length: int = 14
                ) -> dict:
    """RSI candlestick data (open/high/low/close of RSI)."""
    n = len(rsi_values)
    out = {"open": [None] * n, "high": [None] * n,
            "low": [None] * n, "close": [None] * n}
    for i in range(1, n):
        if rsi_values[i] is None or rsi_values[i - 1] is None:
            continue
        out["close"][i] = rsi_values[i]
        out["open"][i] = rsi_values[i - 1]
        out["high"][i] = max(rsi_values[i], rsi_values[i - 1])
        out["low"][i] = min(rsi_values[i], rsi_values[i - 1])
    return out


def ob_os_signals(rsi_values: list[Optional[float]],
                  ob_level: float = 70, os_level: float = 30) -> dict:
    """Overbought/Oversold crossover signals."""
    n = len(rsi_values)
    ob = [False] * n
    os_sig = [False] * n
    for i in range(1, n):
        if rsi_values[i] is None or rsi_values[i - 1] is None:
            continue
        ob[i] = rsi_values[i - 1] < ob_level <= rsi_values[i]
        os_sig[i] = rsi_values[i - 1] > os_level >= rsi_values[i]
    return {"overbought": ob, "oversold": os_sig}


def rsi_histogram(rsi_values: list[Optional[float]],
                  ma_values: list[Optional[float]]) -> list[Optional[float]]:
    """RSI - MA histogram."""
    return [
        (rsi_values[i] - ma_values[i]) if (rsi_values[i] is not None and ma_values[i] is not None) else None
        for i in range(len(rsi_values))
    ]