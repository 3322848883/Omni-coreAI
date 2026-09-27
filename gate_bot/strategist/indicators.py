"""Local technical indicators for strategist snapshots and triggers.

Supported names (custom periods):
  emaN / rsiN / atrN / maN / smaN / rmaN / wmaN / vwmaN
  atrN_rma|sma|ema|wma   (Pine ATR smoothing; default RMA = Wilder)
  macd | macd_dea | macd_hist | macd_difference
  macdF_S_SIG   (e.g. macd12_26_9 → dif=EMA12-EMA26, dea=EMA9(dif))
  bollN | bollN_K | boll | boll_upper|middle|lower | boll_*_band
  stochN | stoch_kN | stoch_dN | cciN | wrN | mfiN | adxN
  vwapN | obv | supertrend | supertrendN

EMA k=2/(n+1); ATR/RMA Wilder — Pine ta.tr(true)+ta.rma.
Unknown names raise ValueError (no silent null).
"""
from __future__ import annotations

import re
from typing import Any, Optional

__all__ = [
    "ema",
    "sma",
    "rma",
    "wma",
    "vwma",
    "stdev",
    "rsi",
    "rsi_smoothed",
    "rsi_bollinger",
    "atr",
    "true_range",
    "macd",
    "boll",
    "ema_smooth",
    "ema_boll",
    "stochastic",
    "cci",
    "williams_r",
    "mfi",
    "adx",
    "vwap",
    "obv",
    "supertrend",
    "highest",
    "lowest",
    "linreg",
    "squeeze_momentum",
    "parse_indicator_name",
    "attach_indicators",
    "latest_indicators",
    "IndicatorNameError",
]


class IndicatorNameError(ValueError):
    pass


def ema(closes: list[float], period: int) -> list[Optional[float]]:
    """Standard EMA (k=2/(period+1)), SMA-seeded at index period-1."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    sma0 = sum(closes[:period]) / period
    out[period - 1] = sma0
    prev = sma0
    for i in range(period, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def sma(values: list[float], period: int) -> list[Optional[float]]:
    """Simple moving average."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    run = sum(values[:period])
    out[period - 1] = run / period
    for i in range(period, n):
        run += values[i] - values[i - period]
        out[i] = run / period
    return out


def rma(values: list[float], period: int) -> list[Optional[float]]:
    """Wilder RMA / SMMA: seed=mean(first period), then prev*(n-1)/n + x/n."""
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


def wma(values: list[float], period: int) -> list[Optional[float]]:
    """Weighted MA: weights 1..period (Pine ta.wma)."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    denom = period * (period + 1) / 2
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        out[i] = sum((j + 1) * v for j, v in enumerate(window)) / denom
    return out


def vwma(values: list[float], volumes: list[float], period: int) -> list[Optional[float]]:
    """Volume-weighted MA: sum(price*vol)/sum(vol)."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        pv = values[i - period + 1 : i + 1]
        vv = volumes[i - period + 1 : i + 1]
        sv = sum(vv)
        out[i] = (sum(p * v for p, v in zip(pv, vv)) / sv) if sv else None
    return out


def stdev(values: list[float], period: int) -> list[Optional[float]]:
    """Population standard deviation over rolling window (Pine ta.stdev default)."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        out[i] = var ** 0.5
    return out


def ema_smooth(
    values: list[float],
    ema_len: int,
    ma_len: int = 14,
    ma_type: str = "ema",
    volumes: Optional[list[float]] = None,
) -> list[Optional[float]]:
    """Pine: EMA(src, len) then smooth with SMA/EMA/RMA/WMA/VWMA."""
    base = ema(values, ema_len)
    t = (ma_type or "ema").lower()
    # smooth only the EMA tail; pass through Nones
    xs = [v for v in base]
    filled = [x if x is not None else 0.0 for x in xs]
    if t == "sma":
        s = sma(filled, ma_len)
    elif t in ("smma", "rma"):
        s = rma(filled, ma_len)
    elif t == "wma":
        s = wma(filled, ma_len)
    elif t == "vwma" and volumes:
        s = vwma(filled, list(volumes), ma_len)
    else:
        s = ema(filled, ma_len)
    # mask where base EMA is None
    return [None if base[i] is None else s[i] for i in range(len(base))]


def ema_boll(
    values: list[float],
    ema_len: int = 20,
    ma_len: int = 14,
    k: float = 2.0,
    ma_type: str = "ema",
    volumes: Optional[list[float]] = None,
) -> dict[str, list[Optional[float]]]:
    """Pine EMA + smoothing + BB on the (smoothed) EMA series."""
    src = ema_smooth(values, ema_len, ma_len, ma_type, volumes)
    base = ema(values, ema_len)
    middle = src
    filled = [x if x is not None else 0.0 for x in base]
    sd = stdev(filled, ma_len)
    n = len(values)
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(n):
        if middle[i] is None or sd[i] is None:
            continue
        upper[i] = middle[i] + k * sd[i]
        lower[i] = middle[i] - k * sd[i]
    return {"middle": middle, "upper": upper, "lower": lower, "ema": base}


def rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Pine RSI: up/down = ta.rma of change; rsi = down==0?100: up==0?0:100-100/(1+up/down).

    First value at index `period` (change is na at bar 0 → period change bars).
    """
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period + 1:
        return out
    # ta.change: bar0 = na; bar i>=1 = close[i]-close[i-1]
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))     # math.max(change, 0)
        losses.append(max(-ch, 0.0))   # -math.min(change, 0)
    up = rma(gains, period)
    down = rma(losses, period)
    for i in range(n):
        u = up[i - 1] if i >= 1 else None  # gains index i-1 ↔ close index i
        d = down[i - 1] if i >= 1 else None
        if u is None or d is None:
            continue
        if d == 0:
            out[i] = 100.0
        elif u == 0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - (100.0 / (1.0 + u / d))
    return out


def _ma_series(values: list[Optional[float]], period: int, ma_type: str) -> list[Optional[float]]:
    """MA over a series that may contain None (None treated as 0 skip → None out)."""
    t = (ma_type or "sma").strip().lower()
    filled = [0.0 if v is None else float(v) for v in values]
    mask = [v is not None for v in values]
    if t == "sma":
        s = sma(filled, period)
    elif t == "ema":
        s = ema(filled, period)
    elif t in ("rma", "smma"):
        s = rma(filled, period)
    elif t == "wma":
        s = wma(filled, period)
    elif t == "vwma":
        # VWMA of RSI needs volume — fall back to SMA of values when no vol
        s = sma(filled, period)
    else:
        raise ValueError(f"ma_type must be sma|ema|rma|wma|vwma, got {ma_type!r}")
    return [None if not mask[i] else s[i] for i in range(len(values))]


def rsi_smoothed(
    closes: list[float], period: int = 14, ma_type: str = "sma", ma_len: int = 14
) -> list[Optional[float]]:
    """Pine RSI + smoothing MA: ma(rsi, maLen, maType)."""
    base = rsi(closes, period)
    return _ma_series(base, max(1, ma_len), ma_type)


def rsi_bollinger(
    closes: list[float], period: int = 14, ma_len: int = 14, k: float = 2.0
) -> dict[str, list[Optional[float]]]:
    """Pine 'SMA + Bollinger Bands' on RSI: middle=SMA(rsi), bands=middle±k*stdev(rsi)."""
    base = rsi(closes, period)
    n = len(base)
    middle = _ma_series(base, max(1, ma_len), "sma")
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(n):
        m = middle[i]
        if m is None:
            continue
        window = [base[j] for j in range(i - ma_len + 1, i + 1) if 0 <= j < n and base[j] is not None]
        if len(window) < ma_len:
            continue
        mean = sum(window) / ma_len
        var = sum((x - mean) ** 2 for x in window) / ma_len
        sd = var ** 0.5
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return {"middle": middle, "upper": upper, "lower": lower}


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """Pine ta.tr(true): bar0 = high-low; later = max(h-l, |h-pc|, |l-pc|)."""
    n = len(highs)
    trs: list[float] = []
    for i in range(n):
        h, l = highs[i], lows[i]
        if i == 0:
            trs.append(h - l)
        else:
            pc = closes[i - 1]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
    smoothing: str = "rma",
) -> list[Optional[float]]:
    """Pine ATR: ma_function(ta.tr(true), length).

    smoothing: rma (Wilder, default) | sma | ema | wma  — matches Pine input.options.
    """
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < 1 or period <= 0:
        return out
    trs = true_range(highs, lows, closes)
    sm = (smoothing or "rma").strip().lower()
    if sm == "rma":
        return rma(trs, period)
    if sm == "sma":
        return sma(trs, period)
    if sm == "ema":
        return ema(trs, period)
    if sm == "wma":
        return wma(trs, period)
    raise ValueError(f"atr smoothing must be rma|sma|ema|wma, got {smoothing!r}")


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    osc_type: str = "ema",
    sig_type: str = "ema",
) -> dict[str, list[Optional[float]]]:
    """Pine MACD: macd = ma(fast)-ma(slow); signal = ma(macd, sig); hist = macd-signal.

    osc_type / sig_type: ema (default) | sma — matches Pine Oscillator/Signal MA type.
    """
    def _ma(src: list[float], length: int, kind: str) -> list[Optional[float]]:
        k = (kind or "ema").strip().lower()
        if k == "sma":
            return sma(src, length)
        return ema(src, length)

    ef = _ma(closes, fast, osc_type)
    es = _ma(closes, slow, osc_type)
    dif: list[Optional[float]] = []
    for a, b in zip(ef, es):
        dif.append(None if a is None or b is None else a - b)
    # signal MA of dif (None-safe)
    st = (sig_type or "ema").strip().lower()
    dea: list[Optional[float]] = [None] * len(dif)
    if st == "sma":
        run = 0.0
        cnt = 0
        for i, d in enumerate(dif):
            if d is not None:
                run += d
                cnt += 1
            if i >= signal and dif[i - signal] is not None:
                run -= dif[i - signal]  # type: ignore[operator]
                cnt -= 1
            if cnt >= signal:
                dea[i] = run / signal
    else:
        k = 2.0 / (signal + 1)
        state: Optional[float] = None
        seed: list[float] = []
        for i, d in enumerate(dif):
            if d is None:
                continue
            if state is None:
                seed.append(d)
                if len(seed) >= signal:
                    state = sum(seed) / signal
                    dea[i] = state
            else:
                state = d * k + state * (1.0 - k)
                dea[i] = state
    hist: list[Optional[float]] = [
        None if (d is None or s is None) else d - s for d, s in zip(dif, dea)
    ]
    return {"dif": dif, "dea": dea, "hist": hist}


def boll(
    closes: list[float],
    period: int = 20,
    k: float = 2.0,
    ma_type: str = "sma",
    volumes: Optional[list[float]] = None,
) -> dict[str, list[Optional[float]]]:
    """Pine Bollinger Bands: basis=ma(src,len,type); dev=mult*ta.stdev(src,len).

    ma_type: sma (default) | ema | smma|rma | wma | vwma  — matches Pine Basis MA Type.
    ta.stdev is population std of **src** (window mean), not of the basis — 照 Pine。
    """
    n = len(closes)
    t = (ma_type or "sma").strip().lower()
    if t == "sma":
        basis = sma(closes, period)
    elif t == "ema":
        basis = ema(closes, period)
    elif t in ("rma", "smma"):
        basis = rma(closes, period)
    elif t == "wma":
        basis = wma(closes, period)
    elif t == "vwma":
        basis = vwma(closes, list(volumes or [1.0] * n), period)
    else:
        raise ValueError(f"boll ma_type must be sma|ema|rma|wma|vwma, got {ma_type!r}")
    sd = stdev(closes, period)  # ta.stdev(src, length) — population
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(n):
        b, s = basis[i], sd[i]
        if b is None or s is None:
            continue
        dev = k * s
        upper[i] = b + dev
        lower[i] = b - dev
    return {"upper": upper, "middle": basis, "lower": lower}


# ── 追加指标（Pine 对齐）────────────────────────────────────────────
def stochastic(
    highs: list[float], lows: list[float], closes: list[float],
    k_period: int = 14, k_smooth: int = 3, d_period: int = 3,
) -> dict[str, list[Optional[float]]]:
    """Pine ta.stoch: raw %K = 100*(c-lowest)/(highest-lowest); K=SMA(raw,k_smooth); D=SMA(K,d)."""
    n = len(closes)
    raw: list[Optional[float]] = [None] * n
    for i in range(n):
        if i + 1 < k_period:
            continue
        hh = max(highs[i - k_period + 1 : i + 1])
        ll = min(lows[i - k_period + 1 : i + 1])
        rng = hh - ll
        raw[i] = 100.0 if rng == 0 else 100.0 * (closes[i] - ll) / rng
    # SMA smoothing of raw (None-safe)
    def _sma_none(vals: list[Optional[float]], period: int) -> list[Optional[float]]:
        out: list[Optional[float]] = [None] * len(vals)
        run = 0.0
        cnt = 0
        for i, v in enumerate(vals):
            if v is not None:
                run += v
                cnt += 1
            if i >= period and vals[i - period] is not None:
                run -= vals[i - period]  # type: ignore[operator]
                cnt -= 1
            if cnt >= period:
                out[i] = run / period
        return out

    k_line = _sma_none(raw, max(1, k_smooth))
    d_line = _sma_none(k_line, max(1, d_period))
    return {"k": k_line, "d": d_line}


def cci(
    highs: list[float], lows: list[float], closes: list[float], period: int = 20
) -> list[Optional[float]]:
    """Pine ta.cci: (tp - SMA(tp)) / (0.015 * mean deviation), tp=(h+l+c)/3."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    tp = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    for i in range(n):
        if i + 1 < period:
            continue
        window = tp[i - period + 1 : i + 1]
        sma_tp = sum(window) / period
        md = sum(abs(x - sma_tp) for x in window) / period
        out[i] = 0.0 if md == 0 else (tp[i] - sma_tp) / (0.015 * md)
    return out


def williams_r(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[Optional[float]]:
    """Williams %R: -100*(hh-c)/(hh-ll)."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    for i in range(n):
        if i + 1 < period:
            continue
        hh = max(highs[i - period + 1 : i + 1])
        ll = min(lows[i - period + 1 : i + 1])
        rng = hh - ll
        out[i] = 0.0 if rng == 0 else -100.0 * (hh - closes[i]) / rng
    return out


def mfi(
    highs: list[float], lows: list[float], closes: list[float],
    volumes: list[float], period: int = 14,
) -> list[Optional[float]]:
    """Money Flow Index: 100 * posMF / (posMF + negMF) over `period`."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < 2:
        return out
    tp = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    rmf = [tp[i] * (volumes[i] or 0.0) for i in range(n)]
    for i in range(period, n):
        pos = 0.0
        neg = 0.0
        for j in range(i - period + 1, i + 1):
            if j == 0:
                continue
            if tp[j] > tp[j - 1]:
                pos += rmf[j]
            elif tp[j] < tp[j - 1]:
                neg += rmf[j]
        if pos + neg == 0:
            out[i] = 50.0
        else:
            out[i] = 100.0 * pos / (pos + neg)
    return out


def adx(
    highs: list[float], lows: list[float], closes: list[float],
    period: int = 14,
) -> dict[str, list[Optional[float]]]:
    """Wilder ADX: +DI/-DI from smoothed TR/DM; ADX = RMA of DX."""
    n = len(closes)
    plus_di: list[Optional[float]] = [None] * n
    minus_di: list[Optional[float]] = [None] * n
    adx_line: list[Optional[float]] = [None] * n
    if n < period * 2:
        return {"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di}
    trs = true_range(highs, lows, closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    # Wilder smooth
    atr_s = rma(trs, period)
    pdm_s = rma(plus_dm, period)
    mdm_s = rma(minus_dm, period)
    dx: list[Optional[float]] = [None] * n
    for i in range(n):
        a, p, m = atr_s[i], pdm_s[i], mdm_s[i]
        if a is None or a == 0 or p is None or m is None:
            continue
        plus_di[i] = 100.0 * p / a
        minus_di[i] = 100.0 * m / a
        s = plus_di[i] + minus_di[i]
        dx[i] = 0.0 if s == 0 else 100.0 * abs(plus_di[i] - minus_di[i]) / s
    # ADX = RMA of dx starting from first non-None run
    valid = [(i, v) for i, v in enumerate(dx) if v is not None]
    if len(valid) >= period:
        vals = [v for _, v in valid]
        idxs = [i for i, _ in valid]
        r = rma(vals, period)
        for j, val in enumerate(r):
            adx_line[idxs[j]] = val
    return {"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di}


def vwap(
    highs: list[float], lows: list[float], closes: list[float],
    volumes: list[float], period: int = 0,
) -> list[Optional[float]]:
    """VWAP: cumulative if period=0 (session-style), else rolling SMA of (h+l+c)/3 volume-weighted."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    tp = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    if period <= 0:
        cum_pv = 0.0
        cum_v = 0.0
        for i in range(n):
            v = volumes[i] or 0.0
            cum_pv += tp[i] * v
            cum_v += v
            out[i] = (cum_pv / cum_v) if cum_v else None
        return out
    for i in range(n):
        if i + 1 < period:
            continue
        pv = 0.0
        sv = 0.0
        for j in range(i - period + 1, i + 1):
            v = volumes[j] or 0.0
            pv += tp[j] * v
            sv += v
        out[i] = (pv / sv) if sv else None
    return out


def obv(closes: list[float], volumes: list[float]) -> list[Optional[float]]:
    """On-Balance Volume."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n == 0:
        return out
    total = 0.0
    out[0] = 0.0
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            total += volumes[i] or 0.0
        elif closes[i] < closes[i - 1]:
            total -= volumes[i] or 0.0
        out[i] = total
    return out


def supertrend(
    highs: list[float], lows: list[float], closes: list[float],
    period: int = 10, mult: float = 3.0,
) -> dict[str, list[Optional[float]]]:
    """Pine SuperTrend: upper/lower band from HL2 ± mult*ATR, flip on close cross."""
    n = len(closes)
    st: list[Optional[float]] = [None] * n
    direction: list[Optional[float]] = [None] * n
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    a = atr(highs, lows, closes, period, smoothing="rma")
    hl2 = [(h + l) / 2.0 for h, l in zip(highs, lows)]
    prev_upper: Optional[float] = None
    prev_lower: Optional[float] = None
    prev_dir = 1
    prev_close: Optional[float] = None
    for i in range(n):
        if a[i] is None:
            continue
        basic_u = hl2[i] + mult * a[i]
        basic_l = hl2[i] - mult * a[i]
        u = basic_u if prev_upper is None or prev_close is None or prev_close > prev_upper \
            else max(basic_u, prev_upper)
        l = basic_l if prev_lower is None or prev_close is None or prev_close < prev_lower \
            else min(basic_l, prev_lower)
        upper[i] = u
        lower[i] = l
        d = prev_dir
        if prev_close is not None:
            if prev_close > u:
                d = -1
            elif prev_close < l:
                d = 1
        direction[i] = d
        st[i] = l if d == 1 else u
        prev_upper, prev_lower, prev_dir, prev_close = u, l, d, closes[i]
    return {"supertrend": st, "direction": direction, "upper": upper, "lower": lower}


def highest(values: list[float], period: int) -> list[Optional[float]]:
    """Pine ta.highest: rolling max over `period`."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0:
        return out
    for i in range(n):
        if i + 1 < period:
            continue
        out[i] = max(values[i - period + 1 : i + 1])
    return out


def lowest(values: list[float], period: int) -> list[Optional[float]]:
    """Pine ta.lowest: rolling min over `period`."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0:
        return out
    for i in range(n):
        if i + 1 < period:
            continue
        out[i] = min(values[i - period + 1 : i + 1])
    return out


def linreg(values: list[float], length: int) -> list[Optional[float]]:
    """Pine ta.linreg(source, length, 0): least-squares value at the current bar."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if length <= 1 or n < length:
        return out
    # precompute sum(x), sum(x^2) for x = 0..length-1
    sx = length * (length - 1) / 2.0
    sxx = (length - 1) * length * (2 * length - 1) / 6.0
    denom = length * sxx - sx * sx
    if denom == 0:
        return out
    x_last = length - 1
    for i in range(length - 1, n):
        window = values[i - length + 1 : i + 1]
        sxy = 0.0
        sy = 0.0
        for j, y in enumerate(window):
            sy += y
            sxy += j * y
        slope = (length * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / length
        out[i] = slope * x_last + intercept
    return out


def squeeze_momentum(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    bb_length: int = 20,
    bb_mult: float = 2.0,
    kc_length: int = 20,
    kc_mult: float = 1.5,
    use_true_range: bool = True,
) -> dict[str, list[Optional[float]]]:
    """LazyBear Squeeze Momentum (SQZMOM_LB).

    BB: basis=SMA(close,len); dev=mult*stdev(close,len)   (标准口径用 bb_mult)
    KC: ma=SMA(close,kcLen); rangema=SMA(TR or H-L,kcLen); ±rangema*kc_mult
    sqzOn: lowerBB>lowerKC and upperBB<upperKC
    sqzOff: lowerBB<lowerKC and upperBB>upperKC
    val = linreg(close - avg(avg(highest(h),lowest(l)), SMA(close)), kcLen, 0)
    """
    n = len(closes)
    res = {
        "sqz_mom": [None] * n,
        "sqz_state": [None] * n,   # 1=sqzOn (squeeze), -1=sqzOff, 0=noSqz
        "sqz_on": [None] * n,
        "sqz_off": [None] * n,
        "momentum_up": [None] * n,       # val > 0
        "momentum_increasing": [None] * n,  # val > val[1]
    }
    if n == 0:
        return res
    # BB
    bb_basis = sma(closes, bb_length)
    bb_sd = stdev(closes, bb_length)
    upper_bb = [None] * n
    lower_bb = [None] * n
    for i in range(n):
        if bb_basis[i] is None or bb_sd[i] is None:
            continue
        dev = bb_mult * bb_sd[i]
        upper_bb[i] = bb_basis[i] + dev
        lower_bb[i] = bb_basis[i] - dev
    # KC
    kc_ma = sma(closes, kc_length)
    if use_true_range:
        rng = true_range(highs, lows, closes)
    else:
        rng = [highs[i] - lows[i] for i in range(n)]
    range_ma = sma(rng, kc_length)
    upper_kc = [None] * n
    lower_kc = [None] * n
    for i in range(n):
        if kc_ma[i] is None or range_ma[i] is None:
            continue
        m = range_ma[i] * kc_mult
        upper_kc[i] = kc_ma[i] + m
        lower_kc[i] = kc_ma[i] - m
    # squeeze states
    for i in range(n):
        ubb, lbb, ukc, lkc = upper_bb[i], lower_bb[i], upper_kc[i], lower_kc[i]
        if None in (ubb, lbb, ukc, lkc):
            continue
        on = (lbb > lkc) and (ubb < ukc)
        off = (lbb < lkc) and (ubb > ukc)
        res["sqz_on"][i] = on
        res["sqz_off"][i] = off
        res["sqz_state"][i] = 1 if on else (-1 if off else 0)
    # momentum: linreg(close - avg(avg(highest(h),lowest(l)), sma(close)))
    hh = highest(highs, kc_length)
    ll = lowest(lows, kc_length)
    sm = sma(closes, kc_length)
    diff: list[float] = []
    for i in range(n):
        if hh[i] is None or ll[i] is None or sm[i] is None:
            diff.append(0.0)
        else:
            mid = ((hh[i] + ll[i]) / 2.0 + sm[i]) / 2.0
            diff.append(closes[i] - mid)
    val = linreg(diff, kc_length)
    for i in range(n):
        v = val[i]
        res["sqz_mom"][i] = v
        if v is None:
            continue
        res["momentum_up"][i] = v > 0
        if i > 0 and val[i - 1] is not None:
            res["momentum_increasing"][i] = v > val[i - 1]
    return res


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fill_ema_from(rows, key, period, close_key="c") -> None:
    k = 2.0 / (period + 1)
    state: Optional[float] = None
    closes = [_f(r.get(close_key)) for r in rows]
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
                    s = sum(window) / period
                    rows[i][key] = s
                    state = s
            else:
                rows[i][key] = None
        else:
            state = c * k + state * (1.0 - k)
            rows[i][key] = state


def _fill_sma_from(rows, key, period) -> None:
    closes = [_f(r.get("c")) for r in rows]
    for i in range(len(rows)):
        if _f(rows[i].get(key)) is not None:
            continue
        if i + 1 < period:
            rows[i][key] = None
            continue
        window = closes[i - period + 1 : i + 1]
        if any(x is None for x in window):
            rows[i][key] = None
        else:
            rows[i][key] = sum(window) / period


def _fill_atr_from(rows, key, period, smoothing: str = "rma") -> None:
    """Pine ATR attach: keep DB-provided values; fill gaps from full series."""
    highs = [_f(r.get("h")) or 0.0 for r in rows]
    lows = [_f(r.get("l")) or 0.0 for r in rows]
    closes = [_f(r.get("c")) or 0.0 for r in rows]
    series = atr(highs, lows, closes, period, smoothing=smoothing)
    for i, r in enumerate(rows):
        if _f(r.get(key)) is None:
            r[key] = series[i]


def _fill_rsi_from(rows, key, period) -> None:
    closes = [_f(r.get("c")) for r in rows]
    n = len(rows)
    gains: list[float] = []
    losses: list[float] = []
    avg_gain: Optional[float] = None
    avg_loss: Optional[float] = None
    prev_c: Optional[float] = None
    for i in range(n):
        c = closes[i]
        if c is None:
            rows[i][key] = None
            continue
        if prev_c is None:
            rows[i][key] = None
            prev_c = c
            continue
        # use last known close across gaps (align ATR warm-start)
        ch = c - prev_c
        g, ls = max(ch, 0.0), max(-ch, 0.0)
        gains.append(g)
        losses.append(ls)
        if avg_gain is None:
            if len(gains) < period:
                rows[i][key] = None
            else:
                avg_gain = sum(gains[-period:]) / period
                avg_loss = sum(losses[-period:]) / period
                rows[i][key] = _rsi_value(avg_gain, avg_loss)
        else:
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + ls) / period
            rows[i][key] = _rsi_value(avg_gain, avg_loss)
        prev_c = c


def _fill_macd_from(rows, fast, slow, signal, prefix="", osc_type: str = "ema", sig_type: str = "ema") -> None:
    """Fill macd / macd_dea / macd_hist (or custom keys via mapping in caller)."""
    closes = [_f(r.get("c")) for r in rows]
    series = macd([c if c is not None else 0.0 for c in closes], fast, slow, signal,
                  osc_type=osc_type, sig_type=sig_type)
    keys = {
        "dif": prefix + "macd" if not prefix else "macd",
        "dea": "macd_dea",
        "hist": "macd_hist",
    }
    if prefix == "macd_difference":
        keys["hist"] = "macd_difference"
    for i in range(len(rows)):
        rows[i]["macd"] = series["dif"][i]
        rows[i]["macd_dea"] = series["dea"][i]
        rows[i]["macd_hist"] = series["hist"][i]
        rows[i]["macd_difference"] = series["hist"][i]  # Gate CLI alias


def _fill_boll_from(rows, period, k, only: Optional[str] = None, ma_type: str = "sma") -> None:
    closes = [_f(r.get("c")) for r in rows]
    vols = [_f(r.get("v")) for r in rows]
    series = boll([c if c is not None else 0.0 for c in closes], period, k,
                  ma_type=ma_type,
                  volumes=[v if v is not None else 0.0 for v in vols])
    mapping = {
        "upper": "boll_upper",
        "middle": "boll_middle",
        "lower": "boll_lower",
    }
    for i in range(len(rows)):
        for part, key in mapping.items():
            if only and part != only:
                continue
            rows[i][key] = series[part][i]
            # Gate CLI aliases
            rows[i][key + "_band"] = series[part][i]


_MACD_RE = re.compile(r"^macd(?:_(dea|hist|difference))?$")
_MACD_PARAM_RE = re.compile(r"^macd(\d+)_(\d+)_(\d+)$")
# boll20 | boll20_2 | boll20_ema | boll20_ema_2 | boll_ema ...
_BOLL_RE = re.compile(
    r"^boll(?:(\d+)(?:_(sma|ema|rma|smma|wma|vwma))?(?:_(\d+(?:\.\d+)?))?)?$"
)
_BOLL_PART_RE = re.compile(r"^boll_(upper|middle|lower)(?:_band)?$")
_BOLL_MA_PART_RE = re.compile(r"^boll_(sma|ema|rma|smma|wma|vwma)_(upper|middle|lower)(?:_band)?$")
# boll20_ema_upper / boll20_ema_upper_band / boll20_ema_2_upper
_BOLL_FULL_RE = re.compile(
    r"^boll(\d+)_(sma|ema|rma|smma|wma|vwma)(?:_(\d+(?:\.\d+)?))?_(upper|middle|lower)(?:_band)?$"
)


def parse_indicator_name(name: str) -> dict[str, Any]:
    """Parse indicator name → spec dict; raise IndicatorNameError if unknown."""
    n = (name or "").strip().lower()

    # ATR with Pine smoothing: atr14 | atr14_rma|sma|ema|wma
    m = re.match(r"^atr(\d+)(?:_(rma|sma|ema|wma))?$", n)
    if m:
        return {"kind": "atr", "period": int(m.group(1)),
                "smoothing": m.group(2) or "rma", "name": n}

    # RSI with Pine smoothing / BB-on-RSI:
    #   rsi14 | rsi14_sma|ema|rma|wma|vwma | rsi14_bb
    m = re.match(r"^rsi(\d+)(?:_(sma|ema|rma|wma|vwma|smma|bb))?$", n)
    if m:
        period = int(m.group(1))
        tag = m.group(2)
        if tag == "bb":
            return {"kind": "rsi_bb", "period": period, "ma_len": period, "k": 2.0, "name": n}
        if tag:
            ma = "rma" if tag == "smma" else tag
            return {"kind": "rsi_smooth", "period": period, "ma_type": ma, "ma_len": period, "name": n}
        return {"kind": "rsi", "period": period, "name": n}

    for kind in ("ema", "ma", "sma", "rma", "wma", "vwma"):
        if n.startswith(kind) and n[len(kind) :].isdigit():
            return {"kind": kind, "period": int(n[len(kind) :]), "name": n}

    # 新指标：stochN / stoch_kN / stoch_dN / cciN / wrN / mfiN / adxN
    m = re.match(r"^stoch_k(\d+)$", n)
    if m:
        return {"kind": "stochastic", "k_period": int(m.group(1)), "field": "k", "name": n}
    m = re.match(r"^stoch_d(\d+)$", n)
    if m:
        return {"kind": "stochastic", "k_period": 14, "field": "d", "name": n}
    m = re.match(r"^stoch(\d+)$", n)
    if m:
        return {"kind": "stochastic", "k_period": int(m.group(1)), "field": "k", "name": n}
    m = re.match(r"^cci(\d+)$", n)
    if m:
        return {"kind": "cci", "period": int(m.group(1)), "name": n}
    m = re.match(r"^wr(\d+)$", n)
    if m:
        return {"kind": "wr", "period": int(m.group(1)), "name": n}
    m = re.match(r"^mfi(\d+)$", n)
    if m:
        return {"kind": "mfi", "period": int(m.group(1)), "name": n}
    m = re.match(r"^adx(\d+)$", n)
    if m:
        return {"kind": "adx", "period": int(m.group(1)), "field": "adx", "name": n}
    if n in ("plus_di", "minus_di", "plus_di14", "minus_di14"):
        return {"kind": "adx", "period": 14, "field": "plus_di" if "plus" in n else "minus_di", "name": n}
    m = re.match(r"^vwap(\d+)$", n)
    if m:
        return {"kind": "vwap", "period": int(m.group(1)), "name": n}
    if n == "vwap":
        return {"kind": "vwap", "period": 0, "name": n}
    if n == "obv":
        return {"kind": "obv", "name": n}
    m = re.match(r"^supertrend(\d+)(?:_(\d+(?:\.\d+)?))?$", n)
    if m:
        return {"kind": "supertrend", "period": int(m.group(1)),
                "mult": float(m.group(2) or 3.0), "field": "supertrend", "name": n}
    if n == "supertrend":
        return {"kind": "supertrend", "period": 10, "mult": 3.0, "field": "supertrend", "name": n}

    # LazyBear Squeeze Momentum: sqzmom | sqzmom20 | sqzmom20_20 | sqzmom_state
    if n in ("sqzmom", "sqz_mom", "squeeze", "squeeze_momentum"):
        return {"kind": "sqzmom", "bb_length": 20, "kc_length": 20, "field": "sqz_mom", "name": n}
    if n in ("sqzmom_state", "sqzmomstate", "sqz_mom_state", "sqz_state"):
        return {"kind": "sqzmom", "bb_length": 20, "kc_length": 20, "field": "sqz_state", "name": n}
    if n in ("sqz_on", "sqz_off", "momentum_up", "momentum_increasing"):
        return {"kind": "sqzmom", "bb_length": 20, "kc_length": 20, "field": n, "name": n}
    m = re.match(r"^(?:sqzmom|sqz_mom)(\d+)_(\d+)_(state|mom|on|off|up)$", n)
    if m:
        fld = {"state": "sqz_state", "mom": "sqz_mom", "on": "sqz_on",
               "off": "sqz_off", "up": "momentum_up"}[m.group(3)]
        return {"kind": "sqzmom", "bb_length": int(m.group(1)), "kc_length": int(m.group(2)),
                "field": fld, "name": n}
    m = re.match(r"^(?:sqzmom|sqz_mom)(\d+)(?:_(\d+))?$", n)
    if m:
        bb = int(m.group(1))
        kc = int(m.group(2) or m.group(1))
        return {"kind": "sqzmom", "bb_length": bb, "kc_length": kc, "field": "sqz_mom", "name": n}

    # EMA + smoothing + BB (Pine Moving Average Exponential) — 升级版默认套件
    if n.startswith("ema_smooth"):
        return {"kind": "ema_smooth", "ema_len": 20, "ma_len": 14, "ma_type": "ema", "name": n}
    if n.startswith("ema_boll"):
        return {"kind": "ema_boll", "ema_len": 20, "ma_len": 14, "k": 2.0, "field": "upper",
                "ma_type": "ema", "name": n}
    # pine_ema: 一次产出 ema20 + smooth + bands（替代旧单线 EMA 默认用法）
    if n in ("pine_ema", "ema_study", "pine_ema20"):
        return {"kind": "pine_ema", "ema_len": 20, "ma_len": 14, "k": 2.0, "ma_type": "ema", "name": n}

    m = _MACD_PARAM_RE.match(n)
    if m:
        return {
            "kind": "macd",
            "fast": int(m.group(1)),
            "slow": int(m.group(2)),
            "signal": int(m.group(3)),
            "field": "dif",
            "osc_type": "ema",
            "sig_type": "ema",
            "name": n,
        }
    m = re.match(r"^macd(\d+)_(\d+)_(\d+)_(sma|ema)$", n)
    if m:
        # macd12_26_9_sma → oscillator+signal both SMA (Pine default options)
        t = m.group(4)
        return {
            "kind": "macd", "fast": int(m.group(1)), "slow": int(m.group(2)),
            "signal": int(m.group(3)), "field": "dif",
            "osc_type": t, "sig_type": t, "name": n,
        }
    m = _MACD_RE.match(n)
    if m:
        part = m.group(1) or "dif"
        if part == "difference":
            part = "hist"
        field = {"dif": "dif", "dea": "dea", "hist": "hist"}[part]
        return {"kind": "macd", "fast": 12, "slow": 26, "signal": 9,
                "field": field, "osc_type": "ema", "sig_type": "ema", "name": n}
    m = re.match(r"^macd_(sma|ema)(?:_(dea|hist|difference))?$", n)
    if m:
        t = m.group(1)
        part = m.group(2) or "dif"
        if part == "difference":
            part = "hist"
        field = {"dif": "dif", "dea": "dea", "hist": "hist"}[part]
        return {"kind": "macd", "fast": 12, "slow": 26, "signal": 9,
                "field": field, "osc_type": t, "sig_type": t, "name": n}

    m = _BOLL_FULL_RE.match(n)
    if m:
        mat = "rma" if m.group(2) == "smma" else m.group(2)
        return {"kind": "boll", "period": int(m.group(1)), "k": float(m.group(3) or 2),
                "ma_type": mat, "field": m.group(4), "name": n}
    m = _BOLL_MA_PART_RE.match(n)
    if m:
        mat = "rma" if m.group(1) == "smma" else m.group(1)
        return {"kind": "boll", "period": 20, "k": 2.0, "ma_type": mat,
                "field": m.group(2), "name": n}
    m = _BOLL_PART_RE.match(n)
    if m:
        return {"kind": "boll", "period": 20, "k": 2.0, "ma_type": "sma",
                "field": m.group(1), "name": n}
    m = _BOLL_RE.match(n)
    if m:
        period = int(m.group(1) or 20)
        mat = m.group(2) or "sma"
        if mat == "smma":
            mat = "rma"
        k = float(m.group(3) or 2)
        return {"kind": "boll", "period": period, "k": k, "ma_type": mat,
                "field": "all", "name": n}

    raise IndicatorNameError(
        f"unknown indicator {name!r}; supported: emaN/rsiN/atrN/atrN_rma|sma|ema|wma/maN/smaN/rmaN/wmaN/vwmaN, "
        "macd|macd_dea|macd_hist|macdF_S_SIG, "
        "boll[N][_sma|ema|rma|wma|vwma][_K]|boll_upper|middle|lower, "
        "stochN|stoch_kN|stoch_dN|cciN|wrN|mfiN|adxN|vwap[N]|obv|supertrend[N]|sqzmom[N]"
    )


def attach_indicators(rows: list[dict[str, Any]], wanted: list[str] | None = None) -> list[dict[str, Any]]:
    """Attach indicator columns; raises IndicatorNameError on unknown names."""
    if not rows:
        return rows
    wanted = list(wanted or ["ema20", "ema50", "atr14", "rsi14"])
    specs = [parse_indicator_name(n) for n in wanted]

    # dedupe expensive multi-output families
    done_macd: set[tuple] = set()
    done_boll: set[tuple] = set()
    _macd_cache: dict[tuple, dict[str, list]] = {}
    _boll_cache: dict[tuple, dict[str, list]] = {}
    _sqz_cache: dict[tuple, dict[str, list]] = {}
    for spec in specs:
        kind = spec["kind"]
        if kind == "ema":
            _fill_ema_from(rows, spec["name"], spec["period"])
        elif kind in ("ma", "sma"):
            _fill_sma_from(rows, spec["name"], spec["period"])
        elif kind in ("rma", "wma", "vwma"):
            closes = [float(r.get("c") or 0) for r in rows]
            vols = [float(r.get("v") or 0) for r in rows]
            if kind == "rma":
                series = rma(closes, spec["period"])
            elif kind == "wma":
                series = wma(closes, spec["period"])
            else:
                series = vwma(closes, vols, spec["period"])
            for i, r in enumerate(rows):
                r[spec["name"]] = series[i]
        elif kind == "ema_smooth":
            closes = [float(r.get("c") or 0) for r in rows]
            vols = [float(r.get("v") or 0) for r in rows]
            series = ema_smooth(closes, spec["ema_len"], spec["ma_len"], spec.get("ma_type", "ema"), vols)
            for i, r in enumerate(rows):
                r[spec["name"]] = series[i]
        elif kind == "pine_ema":
            # upgraded EMA study: ema + smooth + boll-on-ema (replaces single-line default)
            closes = [float(r.get("c") or 0) for r in rows]
            vols = [float(r.get("v") or 0) for r in rows]
            base = ema(closes, spec["ema_len"])
            sm = ema_smooth(closes, spec["ema_len"], spec["ma_len"], spec.get("ma_type", "ema"), vols)
            bands = ema_boll(closes, spec["ema_len"], spec["ma_len"], spec.get("k", 2.0),
                             spec.get("ma_type", "ema"), vols)
            for i, r in enumerate(rows):
                # keep DB ema20 if present (same contract as _fill_ema_from)
                if r.get("ema20") is None:
                    r["ema20"] = base[i]
                r["ema_smooth"] = sm[i]
                r["ema_boll_middle"] = bands["middle"][i]
                r["ema_boll_upper"] = bands["upper"][i]
                r["ema_boll_lower"] = bands["lower"][i]
        elif kind == "ema_boll":
            key = ("emb", spec["ema_len"], spec["ma_len"], spec.get("k"), spec.get("ma_type"))
            if key not in done_boll:
                closes = [float(r.get("c") or 0) for r in rows]
                vols = [float(r.get("v") or 0) for r in rows]
                bands = ema_boll(closes, spec["ema_len"], spec["ma_len"], spec.get("k", 2.0),
                                 spec.get("ma_type", "ema"), vols)
                for i, r in enumerate(rows):
                    r["ema_boll_middle"] = bands["middle"][i]
                    r["ema_boll_upper"] = bands["upper"][i]
                    r["ema_boll_lower"] = bands["lower"][i]
                done_boll.add(key)
            field = spec.get("field") or "upper"
            src = {"upper": "ema_boll_upper", "lower": "ema_boll_lower",
                   "middle": "ema_boll_middle"}[field]
            for i, r in enumerate(rows):
                r[spec["name"]] = r.get(src)
        elif kind == "atr":
            _fill_atr_from(rows, spec["name"], spec["period"], smoothing=spec.get("smoothing") or "rma")
        elif kind == "rsi":
            _fill_rsi_from(rows, spec["name"], spec["period"])
        elif kind == "rsi_smooth":
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            series = rsi_smoothed(closes, spec["period"], spec.get("ma_type") or "sma",
                                  spec.get("ma_len") or spec["period"])
            for i, r in enumerate(rows):
                r[spec["name"]] = series[i]
        elif kind == "rsi_bb":
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            res = rsi_bollinger(closes, spec["period"], spec.get("ma_len") or spec["period"],
                                spec.get("k") or 2.0)
            for i, r in enumerate(rows):
                r[f"{spec['name']}_middle"] = res["middle"][i]
                r[f"{spec['name']}_upper"] = res["upper"][i]
                r[f"{spec['name']}_lower"] = res["lower"][i]
                r[spec["name"]] = res["middle"][i]  # alias middle
        elif kind == "stochastic":
            highs = [_f(r.get("h")) or 0.0 for r in rows]
            lows = [_f(r.get("l")) or 0.0 for r in rows]
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            res = stochastic(highs, lows, closes, k_period=spec.get("k_period") or 14)
            src = "k" if spec.get("field", "k") == "k" else "d"
            for i, r in enumerate(rows):
                r[spec["name"]] = res[src][i]
        elif kind == "cci":
            highs = [_f(r.get("h")) or 0.0 for r in rows]
            lows = [_f(r.get("l")) or 0.0 for r in rows]
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            series = cci(highs, lows, closes, spec["period"])
            for i, r in enumerate(rows):
                r[spec["name"]] = series[i]
        elif kind == "wr":
            highs = [_f(r.get("h")) or 0.0 for r in rows]
            lows = [_f(r.get("l")) or 0.0 for r in rows]
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            series = williams_r(highs, lows, closes, spec["period"])
            for i, r in enumerate(rows):
                r[spec["name"]] = series[i]
        elif kind == "mfi":
            highs = [_f(r.get("h")) or 0.0 for r in rows]
            lows = [_f(r.get("l")) or 0.0 for r in rows]
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            vols = [_f(r.get("v")) or 0.0 for r in rows]
            series = mfi(highs, lows, closes, vols, spec["period"])
            for i, r in enumerate(rows):
                r[spec["name"]] = series[i]
        elif kind == "adx":
            highs = [_f(r.get("h")) or 0.0 for r in rows]
            lows = [_f(r.get("l")) or 0.0 for r in rows]
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            res = adx(highs, lows, closes, spec.get("period") or 14)
            src = spec.get("field") or "adx"
            for i, r in enumerate(rows):
                r[spec["name"]] = res[src][i]
        elif kind == "vwap":
            highs = [_f(r.get("h")) or 0.0 for r in rows]
            lows = [_f(r.get("l")) or 0.0 for r in rows]
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            vols = [_f(r.get("v")) or 0.0 for r in rows]
            series = vwap(highs, lows, closes, vols, spec.get("period") or 0)
            for i, r in enumerate(rows):
                r[spec["name"]] = series[i]
        elif kind == "obv":
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            vols = [_f(r.get("v")) or 0.0 for r in rows]
            series = obv(closes, vols)
            for i, r in enumerate(rows):
                r[spec["name"]] = series[i]
        elif kind == "supertrend":
            highs = [_f(r.get("h")) or 0.0 for r in rows]
            lows = [_f(r.get("l")) or 0.0 for r in rows]
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            res = supertrend(highs, lows, closes, spec.get("period") or 10, spec.get("mult") or 3.0)
            field = spec.get("field") or "supertrend"
            src = {"supertrend": "supertrend", "direction": "direction",
                   "upper": "upper", "lower": "lower"}.get(field, "supertrend")
            for i, r in enumerate(rows):
                r[spec["name"]] = res[src][i]
        elif kind == "sqzmom":
            highs = [_f(r.get("h")) or 0.0 for r in rows]
            lows = [_f(r.get("l")) or 0.0 for r in rows]
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            key = (spec.get("bb_length") or 20, spec.get("kc_length") or 20)
            if key not in _sqz_cache:
                _sqz_cache[key] = squeeze_momentum(
                    highs, lows, closes,
                    bb_length=spec.get("bb_length") or 20,
                    kc_length=spec.get("kc_length") or 20,
                )
            series = _sqz_cache[key]
            field = spec.get("field") or "sqz_mom"
            src = {"sqz_mom": "sqz_mom", "sqz_state": "sqz_state",
                   "sqz_on": "sqz_on", "sqz_off": "sqz_off",
                   "momentum_up": "momentum_up",
                   "momentum_increasing": "momentum_increasing"}.get(field, "sqz_mom")
            for i, r in enumerate(rows):
                r[spec["name"]] = series[src][i]
            # 默认名额外补一组友好列
            if spec["name"] in ("sqzmom", "sqz_mom"):
                for i, r in enumerate(rows):
                    r["sqz_state"] = series["sqz_state"][i]
                    r["sqz_on"] = series["sqz_on"][i]
                    r["momentum_up"] = series["momentum_up"][i]
                    r["momentum_increasing"] = series["momentum_increasing"][i]
        elif kind == "macd":
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            o_t = spec.get("osc_type") or "ema"
            s_t = spec.get("sig_type") or "ema"
            key = (spec["fast"], spec["slow"], spec["signal"], o_t, s_t)
            if key not in done_macd:
                series = macd(closes, spec["fast"], spec["slow"], spec["signal"],
                              osc_type=o_t, sig_type=s_t)
                done_macd.add(key)
                # 缓存本次家族，后续同参 spec 直接复用
                _macd_cache[key] = series
            series = _macd_cache[key]
            field = spec["field"]
            src = {"dif": "dif", "dea": "dea", "hist": "hist"}[field]
            for i, r in enumerate(rows):
                r[spec["name"]] = series[src][i]
            # 默认 EMA 家族写共享列（兼容旧名 macd/macd_dea/macd_hist）
            if o_t == "ema" and s_t == "ema":
                for i, r in enumerate(rows):
                    r["macd"] = series["dif"][i]
                    r["macd_dea"] = series["dea"][i]
                    r["macd_hist"] = series["hist"][i]
                    r["macd_difference"] = series["hist"][i]
        elif kind == "boll":
            closes = [_f(r.get("c")) or 0.0 for r in rows]
            vols = [_f(r.get("v")) or 0.0 for r in rows]
            mat = spec.get("ma_type") or "sma"
            key = (spec["period"], spec["k"], mat)
            if key not in done_boll:
                series = boll(closes, spec["period"], spec["k"], ma_type=mat, volumes=vols)
                _boll_cache[key] = series
                done_boll.add(key)
            series = _boll_cache[key]
            field = spec["field"]
            if field == "all":
                # `boll20` column aliases middle (basis) band for latest_indicators
                for i, r in enumerate(rows):
                    r[spec["name"]] = series["middle"][i]
            else:
                for i, r in enumerate(rows):
                    r[spec["name"]] = series[field][i]
            # 默认 SMA 家族写共享列（兼容 boll_upper / boll_*_band）
            if mat == "sma":
                for i, r in enumerate(rows):
                    r["boll_upper"] = series["upper"][i]
                    r["boll_middle"] = series["middle"][i]
                    r["boll_lower"] = series["lower"][i]
                    r["boll_upper_band"] = series["upper"][i]
                    r["boll_middle_band"] = series["middle"][i]
                    r["boll_lower_band"] = series["lower"][i]
    return rows


def latest_indicators(rows: list[dict[str, Any]], wanted: list[str] | None = None) -> dict[str, Any]:
    wanted = list(wanted or ["ema20", "ema50", "atr14", "rsi14"])
    if not rows:
        return {k: None for k in wanted}
    last = rows[-1]
    return {name: last.get(name) for name in wanted}
