"""策略绩效指标与评分板（只读 account.db / trials，不改撮合）。"""
from .stats import (  # noqa: F401
    calmar,
    deflated_sharpe,
    expectancy,
    kurtosis,
    max_drawdown_pct,
    profit_factor,
    probabilistic_sharpe,
    returns_from_equity,
    sharpe,
    skewness,
    sortino,
    win_rate,
)
from .trials import scan_trials, trial_counts  # noqa: F401
from .scoreboard import build_scoreboard, format_table  # noqa: F401
