"""TV Indicator 3: Linear Regression Heikin Ashi Candles (B3AR_Trades)

LR HA Candles + T3 Moving Average + Volatility Bands.
"""
from __future__ import annotations
import math
from typing import Optional


# ── Heikin Ashi ─────────────────────────────────────

def heikin_ashi(opens: list[float], highs: list[float],
                lows: list[float], closes: list[float],
                reduce_lag: bool = True) -> dict:
    """Heikin Ashi candle calculation."""
    n = len(closes)
    ha = {"open": [None] * n, "high": [None] * n,
           "low": [None] * n, "close": [None] * n}
    for i in range(n):
        if i == 0:
            ha["close"][i] = (opens[i] + highs[i] + lows[i] + closes[i]) / 4
            ha["open"][i] = (opens[i] + closes[i]) / 2
        else:
            ha["close"][i] = (opens[i] + highs[i] + lows[i] + closes[i]) / 4
            ha["open"][i] = (ha["open"][i - 1] + ha["close"][i - 1]) / 2
        ha["high"][i] = max(highs[i], ha["open"][i], ha["close"][i])
        ha["low"][i] = min(lows[i], ha["open"][i], ha["close"][i])
    return ha


# ── Linear Regression Candles ────────────────────────

def _linreg_val(values: list[float], period: int, offset: int = 0
                ) -> list[Optional[float]]:
    """Linear regression value at each bar (ta.linreg)."""
    n = len(values)
    out = [None] * n
    for i in range(period - 1, n):
        seg = values[i - period + 1:i + 1]
        sx = sum(range(1, period + 1))
        sy = sum(seg)
        sxy = sum((j + 1) * seg[j] for j in range(period))
        sxx = sum((j + 1) ** 2 for j in range(period))
        denom = period * sxx - sx * sx
        if denom == 0:
            continue
        slope = (period * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / period
        out[i] = intercept + slope * (period + offset)
    return out


def lr_candles(opens: list[float], highs: list[float],
               lows: list[float], closes: list[float],
               length: int = 9) -> dict:
    """Linear Regression candles."""
    return {
        "open": _linreg_val(opens, length),
        "high": _linreg_val(highs, length),
        "low": _linreg_val(lows, length),
        "close": _linreg_val(closes, length),
    }


def lr_ha_candles(opens: list[float], highs: list[float],
                  lows: list[float], closes: list[float],
                  length: int = 9) -> dict:
    """Linear Regression Heikin Ashi candles."""
    ha = heikin_ashi(opens, highs, lows, closes)
    return {
        "open": _linreg_val([v for v in ha["open"]], length),
        "high": _linreg_val([v for v in ha["high"]], length),
        "low": _linreg_val([v for v in ha["low"]], length),
        "close": _linreg_val([v for v in ha["close"]], length),
    }


# ── T3 Moving Average ──────────────────────────────

def _gd(src: list[float], length: int, alpha: float) -> list[Optional[float]]:
    """Generalized DEMA helper."""
    k = 2 / (length + 1)
    # First EMA
    e1 = [None] * len(src)
    e1[0] = src[0]
    for i in range(1, len(src)):
        e1[i] = src[i] * k + e1[i - 1] * (1 - k)
    # Second EMA
    e2 = [None] * len(src)
    e2[0] = e1[0]
    for i in range(1, len(src)):
        e2[i] = e1[i] * k + e2[i - 1] * (1 - k)
    return [(e1[i] * (1 + alpha) - e2[i] * alpha) if e1[i] is not None else None
            for i in range(len(src))]


def t3_moving_average(src: list[float], length: int = 5,
                      alpha: float = 0.7) -> list[Optional[float]]:
    """T3 Moving Average (Tillson). Triple-smoothed with GD."""
    g1 = _gd(src, length, alpha)
    g1_filled = [v if v is not None else 0 for v in g1]
    g2 = _gd(g1_filled, length, alpha)
    g2_filled = [v if v is not None else 0 for v in g2]
    g3 = _gd(g2_filled, length, alpha)
    return g3


# ── Volatility Bands ───────────────────────────────

def _ema(values: list[float], length: int) -> list[Optional[float]]:
    k = 2 / (length + 1)
    out = [None] * len(values)
    if values:
        out[0] = values[0]
        for i in range(1, len(values)):
            out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def _sma(values: list[float], length: int) -> list[Optional[float]]:
    out = [None] * len(values)
    for i in range(length - 1, len(values)):
        out[i] = sum(values[i - length + 1:i + 1]) / length
    return out


def volatility_bands(highs: list[float], lows: list[float],
                     closes: list[float], length: int = 20,
                     upper_inner: float = 2.0, lower_inner: float = 2.0,
                     upper_outer: float = 3.0, lower_outer: float = 3.0,
                     basis_type: str = "EMA") -> dict:
    """Volatility Bands (ATR-based)."""
    n = len(closes)
    # True Range
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    # ATR
    if basis_type == "EMA":
        atr = _ema(tr, length)
    else:
        atr = _sma(tr, length)
    # Basis
    if basis_type == "EMA":
        basis = _ema(closes, length)
    else:
        basis = _sma(closes, length)
    # Bands
    upper_inner_band = [None] * n
    lower_inner_band = [None] * n
    upper_outer_band = [None] * n
    lower_outer_band = [None] * n
    for i in range(n):
        if basis[i] is None or atr[i] is None:
            continue
        upper_inner_band[i] = basis[i] + upper_inner * atr[i]
        lower_inner_band[i] = basis[i] - lower_inner * atr[i]
        upper_outer_band[i] = basis[i] + upper_outer * atr[i]
        lower_outer_band[i] = basis[i] - lower_outer * atr[i]
    return {
        "basis": basis, "atr": atr,
        "upper_inner": upper_inner_band, "lower_inner": lower_inner_band,
        "upper_outer": upper_outer_band, "lower_outer": lower_outer_band,
    }