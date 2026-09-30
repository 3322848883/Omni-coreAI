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
    """StdDev + PearsonR + upper/lower deviations."""
    up_dev = 0.0
    dn_dev = 0.0
    std_acc = 0.0
    dsxx = dsyy = dsxy = 0.0
    periods = length - 1
    da_y = intercept + slope * periods / 2
    val = intercept
    for j in range(periods + 1):
        idx = len(sources) - 1 - j
        if idx < 0:
            break
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
        val += slope
    std_dev = math.sqrt(std_acc / (periods if periods > 0 else 1))
    pearson_r = dsxy / math.sqrt(dsxx * dsyy) if dsxx > 0 and dsyy > 0 else 0.0
    return std_dev, pearson_r, up_dev, dn_dev


def linreg_channel(closes: list[float], highs: list[float], lows: list[float],
                   length: int = 100, upper_mult: float = 3.0,
                   lower_mult: float = 3.0) -> dict:
    """3-layer Linear Regression Channel.

    Returns {base, upper, lower, slope, std_dev, pearson_r}
    """
    slope, average, intercept = calc_slope(closes, length)
    if slope is None:
        return {"base": None, "upper": None, "lower": None,
                "slope": None, "std_dev": None, "pearson_r": None}
    std_dev, pearson_r, up_dev, dn_dev = calc_dev(
        highs, lows, closes, length, slope, average, intercept)
    end_price = intercept
    start_price = intercept + slope * (length - 1)
    return {
        "base": {"start": start_price, "end": end_price},
        "upper": {"start": start_price + upper_mult * std_dev,
                  "end": end_price + upper_mult * std_dev},
        "lower": {"start": start_price - lower_mult * std_dev,
                  "end": end_price - lower_mult * std_dev},
        "slope": slope, "std_dev": std_dev, "pearson_r": pearson_r,
    }


def trendlines(highs: list[float], lows: list[float], closes: list[float],
               opens: list[float], lookback: int = 25) -> dict:
    """Pivot-based trendlines (primary + secondary).

    Returns {"primary": {"upper": {...}, "lower": {...}}, "secondary": {...}}
    """
    def find_pivot_high(data, lb):
        if len(data) < lb * 2 + 1:
            return None, None
        for i in range(len(data) - lb, lb - 1, -1):
            window = data[i - lb:i + lb + 1]
            if data[i] == max(window):
                return data[i], i
        return None, None

    def find_pivot_low(data, lb):
        if len(data) < lb * 2 + 1:
            return None, None
        for i in range(len(data) - lb, lb - 1, -1):
            window = data[i - lb:i + lb + 1]
            if data[i] == min(window):
                return data[i], i
        return None, None

    def calc_trendline(data, pivots_lb, only_up=False):
        ph, ph_idx = find_pivot_high(data, pivots_lb)
        pl, pl_idx = find_pivot_low(data, pivots_lb)
        result = {"upper": None, "lower": None}
        if ph is not None and ph_idx is not None and ph_idx > pivots_lb:
            prev_ph, prev_idx = find_pivot_high(data[:ph_idx], pivots_lb)
            if prev_ph is not None:
                slope = (ph - prev_ph) / max(1, ph_idx - prev_idx)
                result["upper"] = {"start_price": prev_ph, "start_idx": prev_idx,
                                    "slope": slope}
        if pl is not None and pl_idx is not None and pl_idx > pivots_lb:
            prev_pl, prev_idx = find_pivot_low(data[:pl_idx], pivots_lb)
            if prev_pl is not None:
                slope = (pl - prev_pl) / max(1, pl_idx - prev_idx)
                result["lower"] = {"start_price": prev_pl, "start_idx": prev_idx,
                                    "slope": slope}
        return result

    return {
        "primary": calc_trendline(highs, lookback, only_up=False),
        "secondary": calc_trendline(highs, max(2, lookback // 3), only_up=False),
    }