"""Local technical indicators for strategist snapshots and triggers.

Supported names (custom periods):
  emaN / rsiN / atrN / maN / smaN
  macd | macd_dea | macd_hist | macd_difference
  macdF_S_SIG   (e.g. macd12_26_9 → dif=EMA12-EMA26, dea=EMA9(dif))
  bollN | bollN_K | boll | boll_upper|middle|lower | boll_*_band
      (default N=20, K=2; columns boll_upper/boll_middle/boll_lower)

EMA k=2/(n+1); ATR/RSI Wilder — match pa-data-source kline_watcher.
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
    "atr",
    "macd",
    "boll",
    "ema_smooth",
    "ema_boll",
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


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[Optional[float]]]:
    """MACD: dif=EMA(fast)-EMA(slow), dea=EMA(signal) of dif, hist=dif-dea."""
    ef = ema(closes, fast)
    es = ema(closes, slow)
    dif: list[Optional[float]] = []
    for a, b in zip(ef, es):
        dif.append(None if a is None or b is None else a - b)
    # EMA of dif (treat None as skip — seed after first non-None run)
    dea: list[Optional[float]] = [None] * len(dif)
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
    closes: list[float], period: int = 20, k: float = 2.0
) -> dict[str, list[Optional[float]]]:
    """Bollinger: middle=SMA(period), band=middle±k*population std."""
    n = len(closes)
    middle = sma(closes, period)
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(n):
        m = middle[i]
        if m is None:
            continue
        window = closes[i - period + 1 : i + 1]
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return {"upper": upper, "middle": middle, "lower": lower}


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


def _fill_atr_from(rows, key, period) -> None:
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


def _fill_macd_from(rows, fast, slow, signal, prefix="") -> None:
    """Fill macd / macd_dea / macd_hist (or custom keys via mapping in caller)."""
    closes = [_f(r.get("c")) for r in rows]
    series = macd([c if c is not None else 0.0 for c in closes], fast, slow, signal)
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


def _fill_boll_from(rows, period, k, only: Optional[str] = None) -> None:
    closes = [_f(r.get("c")) for r in rows]
    series = boll([c if c is not None else 0.0 for c in closes], period, k)
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
_BOLL_RE = re.compile(r"^boll(?:(\d+)(?:_(\d+(?:\.\d+)?))?)?$")
_BOLL_PART_RE = re.compile(r"^boll_(upper|middle|lower)(?:_band)?$")


def parse_indicator_name(name: str) -> dict[str, Any]:
    """Parse indicator name → spec dict; raise IndicatorNameError if unknown."""
    n = (name or "").strip().lower()
    for kind in ("ema", "rsi", "atr", "ma", "sma", "rma", "wma", "vwma"):
        if n.startswith(kind) and n[len(kind) :].isdigit():
            return {"kind": kind, "period": int(n[len(kind) :]), "name": n}

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
            "name": n,
        }
    m = _MACD_RE.match(n)
    if m:
        part = m.group(1) or "dif"
        if part == "difference":
            part = "hist"
        field = {"dif": "dif", "dea": "dea", "hist": "hist"}[part]
        return {"kind": "macd", "fast": 12, "slow": 26, "signal": 9, "field": field, "name": n}

    m = _BOLL_PART_RE.match(n)
    if m:
        return {
            "kind": "boll",
            "period": 20,
            "k": 2.0,
            "field": m.group(1),
            "name": n,
        }
    m = _BOLL_RE.match(n)
    if m:
        period = int(m.group(1) or 20)
        k = float(m.group(2) or 2)
        return {"kind": "boll", "period": period, "k": k, "field": "all", "name": n}

    raise IndicatorNameError(
        f"unknown indicator {name!r}; supported: emaN/rsiN/atrN/maN/smaN, "
        "macd|macd_dea|macd_hist|macd_difference|macdF_S_SIG, "
        "bollN_K|boll|boll_upper|boll_middle|boll_lower"
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
            _fill_atr_from(rows, spec["name"], spec["period"])
        elif kind == "rsi":
            _fill_rsi_from(rows, spec["name"], spec["period"])
        elif kind == "macd":
            key = (spec["fast"], spec["slow"], spec["signal"])
            if key not in done_macd:
                _fill_macd_from(rows, spec["fast"], spec["slow"], spec["signal"])
                done_macd.add(key)
            # ensure requested alias key exists
            field = spec["field"]
            src = {"dif": "macd", "dea": "macd_dea", "hist": "macd_hist"}[field]
            for i, r in enumerate(rows):
                r[spec["name"]] = r.get(src)
        elif kind == "boll":
            key = (spec["period"], spec["k"])
            if key not in done_boll:
                _fill_boll_from(rows, spec["period"], spec["k"])
                done_boll.add(key)
            field = spec["field"]
            if field == "all":
                # `boll20` column aliases middle band for latest_indicators
                for i, r in enumerate(rows):
                    r[spec["name"]] = r.get("boll_middle")
                continue
            src = f"boll_{field}"
            for i, r in enumerate(rows):
                r[spec["name"]] = r.get(src)
    return rows


def latest_indicators(rows: list[dict[str, Any]], wanted: list[str] | None = None) -> dict[str, Any]:
    wanted = list(wanted or ["ema20", "ema50", "atr14", "rsi14"])
    if not rows:
        return {k: None for k in wanted}
    last = rows[-1]
    return {name: last.get(name) for name in wanted}
