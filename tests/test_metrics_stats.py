"""metrics 纯函数单测。"""
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.metrics.stats import (  # noqa: E402
    calmar,
    deflated_sharpe,
    expectancy,
    max_drawdown_pct,
    profit_factor,
    probabilistic_sharpe,
    returns_from_equity,
    sharpe,
    win_rate,
)


class TestStats(unittest.TestCase):
    def test_returns_from_equity(self):
        r = returns_from_equity([100, 110, 99])
        self.assertEqual(len(r), 2)
        self.assertAlmostEqual(r[0], 0.1)
        self.assertAlmostEqual(r[1], -0.1)

    def test_max_drawdown(self):
        # 100 → 120 → 60 → 90 : peak 120 trough 60 → dd=50%
        self.assertAlmostEqual(max_drawdown_pct([100, 120, 60, 90]), 50.0, places=5)

    def test_sharpe_positive_trend(self):
        eq = [100 + i for i in range(40)]
        s = sharpe(returns_from_equity(eq))
        self.assertGreater(s, 1.0)

    def test_win_rate_pf_expectancy(self):
        realized = [10.0, -5.0, 3.0, -2.0]
        self.assertAlmostEqual(win_rate(realized), 0.5)
        self.assertAlmostEqual(profit_factor(realized), 13 / 7)
        self.assertAlmostEqual(expectancy(realized), 6 / 4)

    def test_calmar(self):
        eq = [100, 110, 105, 120, 130]
        c = calmar(eq)
        self.assertGreater(c, 0.0)

    def test_psr_in_range(self):
        eq = [100 + i * 0.5 for i in range(60)]
        p = probabilistic_sharpe(returns_from_equity(eq))
        self.assertTrue(0.0 <= p <= 1.0)

    def test_dsr_increases_with_trials_penalty(self):
        # 同一收益序列，N 越大 DSR 应不增（期望最大 SR 门槛更高）
        import random
        random.seed(1)
        r = [random.gauss(0.001, 0.01) for _ in range(80)]
        d1 = deflated_sharpe(r, n_trials=1, n_strategies=1)
        d10 = deflated_sharpe(r, n_trials=10, n_strategies=1)
        self.assertGreaterEqual(d1, d10 - 1e-6)

    def test_dsr_bounded(self):
        r = [0.01] * 30
        # 零方差 → 0
        self.assertEqual(deflated_sharpe(r, n_trials=1), 0.0)

    def test_dsr_formula_se_units(self):
        """Bailey：z = srs/se − E[max]；N=1 时 DSR≈PSR(0)。"""
        r = [0.01, -0.005, 0.012, 0.003, -0.002, 0.008, 0.004, -0.001,
             0.006, 0.002, -0.003, 0.009, 0.001, 0.005, -0.004, 0.007,
             0.002, 0.003, -0.001, 0.004]
        psr0 = probabilistic_sharpe(r, sr_threshold=0.0)
        dsr1 = deflated_sharpe(r, n_trials=1, n_strategies=1)
        self.assertAlmostEqual(dsr1, psr0, places=6)
        # N=17 时 DSR 应显著低于 PSR（惩罚到位）
        dsr17 = deflated_sharpe(r, n_trials=17, n_strategies=1)
        self.assertLess(dsr17, psr0 - 0.05)

    def test_sortino_downside_dev(self):
        # textbook: dstd = sqrt(mean(min(r,0)^2))
        r = [0.02, -0.01, 0.03, -0.02, 0.01]
        import math
        dstd = math.sqrt(sum(min(x, 0.0) ** 2 for x in r) / len(r))
        expected = (sum(r) / len(r)) / dstd * math.sqrt(365.0)
        from omnialpha.metrics.stats import sortino
        self.assertAlmostEqual(sortino(r), expected, places=6)


if __name__ == "__main__":
    unittest.main()
