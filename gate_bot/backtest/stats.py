"""统计检验：DSR / p-value / 基准对比。"""
from __future__ import annotations

import math
from typing import Optional


def deflated_sharpe(returns: list[float], n_trials: int = 1) -> float:
    """Deflated Sharpe Ratio（按 trial count 修正选择偏差）。

    DSR = Φ((SR - SR₀) * sqrt(T-1) / sqrt(1 - skew*SR + (kurt-1)/4*SR²))
    SR₀ = E[max SR of n_trials random trials]
    """
    n = len(returns)
    if n < 3:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / max(n - 1, 1)
    std = math.sqrt(var) if var > 0 else 0
    if std <= 0:
        return 0.0
    sr = mean / std * math.sqrt(n)
    # skewness / kurtosis
    skew = sum(((r - mean) / std) ** 3 for r in returns) / n
    kurt = sum(((r - mean) / std) ** 4 for r in returns) / n
    # SR₀（期望最大 Sharpe of n_trials）
    sr0 = math.sqrt(2 * math.log(max(n_trials, 1))) if n_trials > 1 else 0
    # DSR
    denom = abs(1 - skew * sr + (kurt - 3) / 4 * sr * sr) or 1.0
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    return max(0.0, min(1.0, _norm_cdf(z)))


def t_test_pvalue(returns: list[float]) -> float:
    """单样本 t 检验 p-value（H0: mean=0）。双尾。"""
    n = len(returns)
    if n < 2:
        return 1.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        # 零方差：全正收益 → p=0（显著），全零/负 → p=1
        return 0.0 if all(r > 0 for r in returns) else 1.0
    t = mean / (math.sqrt(var) / math.sqrt(n))
    # 近似 p-value（正态近似）
    return 2 * (1 - _norm_cdf(abs(t)))


def buy_and_hold_pnl(closes: list[float]) -> float:
    """Buy & Hold 总收益（百分比）。"""
    if len(closes) < 2:
        return 0.0
    return (closes[-1] - closes[0]) / closes[0] * 100


def sma_crossover_pnl(closes: list[float], fast: int = 50, slow: int = 200) -> float:
    """SMA 交叉策略总收益（百分比）。"""
    if len(closes) < slow:
        return 0.0
    pnl = 0.0
    position = 0  # 0=flat, 1=long
    fast_ma = _sma_series(closes, fast)
    slow_ma = _sma_series(closes, slow)
    for i in range(slow, len(closes)):
        if fast_ma[i] is None or slow_ma[i] is None:
            continue
        if fast_ma[i] > slow_ma[i] and position == 0:
            position = 1
            entry = closes[i]
        elif fast_ma[i] < slow_ma[i] and position == 1:
            pnl += (closes[i] - entry) / entry * 100
            position = 0
    return pnl


def _sma_series(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def _norm_cdf(x: float) -> float:
    """标准正态 CDF 近似。"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
