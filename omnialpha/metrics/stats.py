"""绩效纯函数：收益/回撤/Sharpe 族/交易统计/PSR/DSR。无 IO。"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def returns_from_equity(equity: Sequence[float]) -> list[float]:
    out = []
    for i in range(1, len(equity)):
        prev, cur = float(equity[i - 1]), float(equity[i])
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def max_drawdown_pct(equity: Sequence[float]) -> float:
    peak = None
    max_dd = 0.0
    for v in equity:
        v = float(v)
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd * 100.0


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float], ddof: int = 1) -> float:
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


def sharpe(returns: Sequence[float], periods_per_year: float = 365.0) -> float:
    if len(returns) < 2:
        return 0.0
    s = _std(returns)
    if s <= 0:
        return 0.0
    return _mean(returns) / s * math.sqrt(periods_per_year)


def sortino(returns: Sequence[float], periods_per_year: float = 365.0) -> float:
    if len(returns) < 2:
        return 0.0
    # 下行半标准差 = sqrt(mean(min(r,0)^2))，非带均值样本 std
    downside_sq = [min(r, 0.0) ** 2 for r in returns]
    dstd = math.sqrt(sum(downside_sq) / len(downside_sq))
    if dstd <= 0:
        return 0.0
    return _mean(returns) / dstd * math.sqrt(periods_per_year)


def calmar(equity: Sequence[float], periods_per_year: float = 365.0) -> float:
    rets = returns_from_equity(equity)
    dd = max_drawdown_pct(equity) / 100.0
    if dd <= 0 or not rets:
        return 0.0
    ann = _mean(rets) * periods_per_year
    return ann / dd


def skewness(returns: Sequence[float]) -> float:
    n = len(returns)
    if n < 3:
        return 0.0
    m = _mean(returns)
    s = _std(returns)
    if s <= 0:
        return 0.0
    return sum(((r - m) / s) ** 3 for r in returns) / n


def kurtosis(returns: Sequence[float]) -> float:
    """Excess kurtosis (normal=0)."""
    n = len(returns)
    if n < 4:
        return 0.0
    m = _mean(returns)
    s = _std(returns)
    if s <= 0:
        return 0.0
    return sum(((r - m) / s) ** 4 for r in returns) / n - 3.0


def probabilistic_sharpe(
    returns: Sequence[float],
    sr_threshold: float = 0.0,
    periods_per_year: float = 365.0,
) -> float:
    """PSR vs threshold (Bailey & López de Prado). Returns 0..1 probability."""
    n = len(returns)
    if n < 3:
        return 0.0
    sr = sharpe(returns, periods_per_year)
    # SR in annual terms; SE needs sample SR (non-annual) moments
    srs = _mean(returns) / _std(returns) if _std(returns) > 0 else 0.0
    g3 = skewness(returns)
    g4 = kurtosis(returns)  # excess
    # SE of sample SR (per-period)
    se = math.sqrt(
        max(1e-18, (1.0 - g3 * srs + (g4 - 1.0) / 4.0 * srs**2) / max(n - 1, 1))
    )
    # z of (sr - threshold) using annualized
    sr_thr_per = sr_threshold / math.sqrt(periods_per_year)
    z = (srs - sr_thr_per) / se if se > 0 else 0.0
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _expected_max_sr(n_trials: float, n_strategies: int = 1) -> float:
    """E[max SR] under null for N independent trials (Bailey)."""
    n = max(1.0, float(n_trials) * max(1, n_strategies))
    if n <= 1:
        return 0.0
    # Euler-Mascheroni γ ≈ 0.5772; E[max]≈(1-γ)Φ^{-1}(1-1/N)+γΦ^{-1}(1-1/(N e))
    # Use approximation via inverse normal of 1-1/n
    def inv_norm(p: float) -> float:
        # Acklam approximation (short)
        if p <= 0:
            return -10.0
        if p >= 1:
            return 10.0
        a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
             1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
        b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
             6.680131188771972e01, -1.328068155288572e01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
             -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
        d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
             3.754408661907416e00]
        plow, phigh = 0.02425, 1 - 0.02425
        if p < plow:
            q = math.sqrt(-2 * math.log(p))
            return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
                (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        if p > phigh:
            q = math.sqrt(-2 * math.log(1 - p))
            return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
                (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)

    gamma = 0.5772156649
    z1 = inv_norm(1.0 - 1.0 / n)
    z2 = inv_norm(max(1e-12, 1.0 - 1.0 / (n * math.e)))
    return (1.0 - gamma) * z1 + gamma * z2


def deflated_sharpe(
    returns: Sequence[float],
    n_trials: float = 1.0,
    n_strategies: int = 1,
    periods_per_year: float = 365.0,
) -> float:
    """DSR = P(SR_true > SR*)。

    Bailey & López de Prado：SR* = se · E[max_{i=1..N} Z_i]，故
    z = srs/se − E[max]，其中 srs/se 为样本 SR 的 SE 单位统计量。
    periods_per_year 仅用于兼容签名（阈值在 SE 单位，无需年化换算）。
    """
    if len(returns) < 3:
        return 0.0
    s = _std(returns)
    if s <= 0:
        return 0.0
    n = len(returns)
    srs = _mean(returns) / s
    g3 = skewness(returns)
    g4 = kurtosis(returns)
    se = math.sqrt(
        max(1e-18, (1.0 - g3 * srs + (g4 - 1.0) / 4.0 * srs**2) / max(n - 1, 1))
    )
    # N 个独立试验的期望最大 SR（SE 单位）
    exp_max = _expected_max_sr(n_trials, n_strategies)
    z = srs / se - exp_max
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _round_trips(realised: Sequence[float]) -> list[float]:
    """已平仓每笔 realized_pnl 列表即 round-trip 结果。"""
    return [float(x) for x in realised]


def win_rate(realised: Sequence[float]) -> float:
    rts = _round_trips(realised)
    if not rts:
        return 0.0
    wins = sum(1 for x in rts if x > 0)
    return wins / len(rts)


def profit_factor(realised: Sequence[float]) -> float:
    rts = _round_trips(realised)
    gross_w = sum(x for x in rts if x > 0)
    gross_l = abs(sum(x for x in rts if x < 0))
    if gross_l <= 0:
        return 0.0 if gross_w <= 0 else float("inf") if gross_w > 0 else 0.0
    return gross_w / gross_l


def expectancy(realised: Sequence[float]) -> float:
    rts = _round_trips(realised)
    if not rts:
        return 0.0
    return sum(rts) / len(rts)


def sharpe_from_equity(equity: Sequence[float], periods_per_year: float = 365.0) -> float:
    return sharpe(returns_from_equity(equity), periods_per_year)
