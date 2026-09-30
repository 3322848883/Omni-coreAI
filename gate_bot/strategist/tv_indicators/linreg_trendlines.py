"""TV Indicator 1: Linreg & Trendlines (ParkF)

Linear Regression Channel (3 layers) + Pivot-based Trendlines.
"""
from __future__ import annotations
import math
from typing import Optional


def calc_slope(values: list[float], length: int) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Linear regression: returns (slope, average, intercept)."""
    if len(values) < length or length <= 1:
        return None, None, None
    seg = values[-length:]
    sum_x = sum(range(1, length + 1))
    sum_y = sum(seg)
    sum_xy = sum((i + 1) * seg[i] for i in range(length))
    sum_xx = sum((i + 1) ** 2 for i in range(length))
    denom = length * sum_xx - sum_x * sum_x
    if denom == 0:
        return None, None, None
    slope = (length * sum_xy - sum_x * sum_y) / denom
    average = sum_y / length
    intercept = average - slope * sum_x / length + slope
    return slope, average, intercept


def calc_dev(highs: list[float], lows: list[float], sources: list[float],
             length: int, slope: float, average: float, intercept: float
             ) -> tuple[float, float, float, float]:
    """StdDev + PearsonR + upper/lower deviations.

    `intercept` is the fitted value at the window's oldest bar (x=0);
    fitted value at bar j (0=oldest) is `intercept + slope * j`.
    """
    up_dev = 0.0
    dn_dev = 0.0
    std_acc = 0.0
    dsxx = dsyy = dsxy = 0.0
    periods = length - 1
    # fitted mean of the line over x=0..periods
    da_y = intercept + slope * periods / 2
    base = len(sources) - length
    for j in range(length):
        idx = base + j
        if idx < 0:
            continue
        val = intercept + slope * j
        up_d = highs[idx] - val
        if up_d > up_dev:
            up_dev = up_d
        dn_d = val - lows[idx]
        if dn_d > dn_dev:
            dn_dev = dn_d
        dxt = sources[idx] - average
        dyt = val - da_y
        std_acc += (sources[idx] - val) ** 2
        dsxx += dxt * dxt
        dsyy += dyt * dyt
        dsxy += dxt * dyt
    std_dev = math.sqrt(std_acc / (periods if periods > 0 else 1))
    pearson_r = dsxy / math.sqrt(dsxx * dsyy) if dsxx > 0 and dsyy > 0 else 0.0
    return std_dev, pearson_r, up_dev, dn_dev


def linreg_channel(closes: list[float], highs: list[float], lows: list[float],
                   length: int = 100, upper_mult: float = 3.0,
                   lower_mult: float = 3.0,
                   layers: tuple[float, ...] = (1.0, 2.0, 3.0)) -> dict:
    """3-layer Linear Regression Channel (ParkF).

    `layers` = σ 倍数序列，TV 图上实线/虚线/点线三层对应 1σ/2σ/3σ。
    Returns {base, upper, lower, layers, slope, std_dev, pearson_r}
    """
    slope, average, intercept = calc_slope(closes, length)
    if slope is None:
        return {"base": None, "upper": None, "lower": None, "layers": [],
                "slope": None, "std_dev": None, "pearson_r": None}
    std_dev, pearson_r, up_dev, dn_dev = calc_dev(
        highs, lows, closes, length, slope, average, intercept)
    # intercept = fitted at oldest bar (x=0); newest bar is x=length-1
    start_price = intercept
    end_price = intercept + slope * (length - 1)

    def band(mult: float) -> dict:
        return {
            "start": start_price + mult * std_dev,
            "end": end_price + mult * std_dev,
            "mult": mult,
        }

    def band_dn(mult: float) -> dict:
        return {
            "start": start_price - mult * std_dev,
            "end": end_price - mult * std_dev,
            "mult": mult,
        }

    # 三层：内/中/外（默认 1σ/2σ/3σ），TV 上依次为实线/虚线/点线
    layer_bands = []
    for m in layers:
        layer_bands.append({"upper": band(m), "lower": band_dn(m), "mult": m})

    # 兼容旧字段：upper/lower = 最外层
    outer = layers[-1] if layers else upper_mult
    return {
        "base": {"start": start_price, "end": end_price},
        "upper": {"start": start_price + upper_mult * std_dev,
                  "end": end_price + upper_mult * std_dev},
        "lower": {"start": start_price - lower_mult * std_dev,
                  "end": end_price - lower_mult * std_dev},
        "layers": layer_bands,
        "slope": slope, "std_dev": std_dev, "pearson_r": pearson_r,
    }


def trendlines(highs: list[float], lows: list[float], closes: list[float],
               opens: list[float], lookback: int = 25) -> dict:
    """Pivot-based trendlines (primary + secondary).

    Upper line joins pivot highs (on highs); lower joins pivot lows (on lows).
    Returns {"primary": {"upper": {...}, "lower": {...}}, "secondary": {...}}
    """
    def find_pivot_high(data, lb):
        if len(data) < lb * 2 + 1:
            return None, None
        for i in range(len(data) - lb - 1, lb - 1, -1):
            window = data[i - lb:i + lb + 1]
            if data[i] == max(window):
                return data[i], i
        return None, None

    def find_pivot_low(data, lb):
        if len(data) < lb * 2 + 1:
            return None, None
        for i in range(len(data) - lb - 1, lb - 1, -1):
            window = data[i - lb:i + lb + 1]
            if data[i] == min(window):
                return data[i], i
        return None, None

    def calc_trendline(pivot_series, pivots_lb, use_highs: bool):
        finder = find_pivot_high if use_highs else find_pivot_low
        ph, ph_idx = finder(pivot_series, pivots_lb)
        result = {"upper": None, "lower": None}
        key = "upper" if use_highs else "lower"
        if ph is not None and ph_idx is not None and ph_idx > pivots_lb:
            prev_ph, prev_idx = finder(pivot_series[:ph_idx], pivots_lb)
            if prev_ph is not None and prev_idx is not None:
                slope = (ph - prev_ph) / max(1, ph_idx - prev_idx)
                result[key] = {"start_price": prev_ph, "start_idx": prev_idx,
                               "slope": slope}
        return result

    def layer(lb):
        up = calc_trendline(highs, lb, use_highs=True)
        dn = calc_trendline(lows, lb, use_highs=False)
        return {"upper": up["upper"], "lower": dn["lower"]}

    return {
        "primary": layer(lookback),
        "secondary": layer(max(2, lookback // 3)),
    }