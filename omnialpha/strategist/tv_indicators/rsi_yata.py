"""TV Indicator 2: RSI Yata

Enhanced RSI with smoothing, MA overlay, Bollinger Bands on RSI,
RSI candles, OB/OS dots, RSI/MACD histograms.
"""
from __future__ import annotations
import math
from typing import Optional


def rsi_base(closes: list[float], length: int = 14) -> list[Optional[float]]:
    """Core RSI using RMA smoothing (matches TV ta.rma).

    First value at index `length` (need `length` change bars).
    """
    n = len(closes)
    out = [None] * n
    if n < length + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    def _rsi(u: float, d: float) -> float:
        if d == 0:
            return 100.0
        if u == 0:
            return 0.0
        return 100 - 100 / (1 + u / d)

    # RMA seed = mean of first `length` changes → first RSI at close index `length`
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    out[length] = _rsi(avg_gain, avg_loss)
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        out[i + 1] = _rsi(avg_gain, avg_loss)
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


# ── 市场结构标签（TV 图上 HH/HL/LH/LL）────────────────

def swing_structure(values: list[Optional[float]], left: int = 3,
                    right: int = 3) -> list[Optional[str]]:
    """Pivot 结构标签：HH / HL / LH / LL / H / L。

    在摆动高/低点上标注与前一同类枢轴的关系：
      pivot high: HH（更高）/ LH（更低）/ H（持平）
      pivot low : HL（更高）/ LL（更低）/ L（持平）
    非枢轴位置为 None。left/right 为确认所需左右 bar 数。
    """
    n = len(values)
    out: list[Optional[str]] = [None] * n
    last_ph: Optional[tuple[float, int]] = None
    last_pl: Optional[tuple[float, int]] = None
    for i in range(left, n - right):
        v = values[i]
        if v is None:
            continue
        neighbors = []
        ok = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            x = values[j]
            if x is None:
                ok = False
                break
            neighbors.append(x)
        if not ok or not neighbors:
            continue
        is_ph = v > max(neighbors)
        is_pl = v < min(neighbors)
        if is_ph:
            if last_ph is None:
                out[i] = "H"
            elif v > last_ph[0]:
                out[i] = "HH"
            elif v < last_ph[0]:
                out[i] = "LH"
            else:
                out[i] = "H"
            last_ph = (v, i)
        elif is_pl:
            if last_pl is None:
                out[i] = "L"
            elif v > last_pl[0]:
                out[i] = "HL"
            elif v < last_pl[0]:
                out[i] = "LL"
            else:
                out[i] = "L"
            last_pl = (v, i)
    return out


# ── RSI-MACD（TV 设置里 12/26/9）────────────────────

def rsi_macd(rsi_values: list[Optional[float]], fast: int = 12,
             slow: int = 26, signal: int = 9) -> dict:
    """MACD on RSI series. Returns {macd, signal, hist}."""
    vals = [v if v is not None else 0.0 for v in rsi_values]
    n = len(vals)

    def _ema(src: list[float], length: int) -> list[Optional[float]]:
        if length <= 0 or not src:
            return [None] * n
        k = 2 / (length + 1)
        out: list[Optional[float]] = [None] * n
        out[0] = src[0]
        for i in range(1, n):
            out[i] = src[i] * k + out[i - 1] * (1 - k)
        return out

    ema_f = _ema(vals, fast)
    ema_s = _ema(vals, slow)
    macd = [
        (ema_f[i] - ema_s[i]) if (ema_f[i] is not None and ema_s[i] is not None) else None
        for i in range(n)
    ]
    macd_filled = [v if v is not None else 0.0 for v in macd]
    sig = _ema(macd_filled, signal)
    signal_line = [None if macd[i] is None else sig[i] for i in range(n)]
    hist = [
        (macd[i] - signal_line[i]) if (macd[i] is not None and signal_line[i] is not None) else None
        for i in range(n)
    ]
    return {"macd": macd, "signal": signal_line, "hist": hist}