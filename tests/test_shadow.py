# -*- coding: utf-8 -*-
"""影子对拍统计（pa-data-source/shadow.py）：P1 门禁的判定器。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

from shadow import GATE_RATIO, MISMATCH_TOL, ShadowStats  # noqa: E402


class TestShadowStats(unittest.TestCase):
    def test_identical_values_pass(self):
        st = ShadowStats()
        for i in range(100):
            st.record("ema20", 2400.0 + i, 2400.0 + i, t=i)
        self.assertEqual(st.n, 100)
        self.assertEqual(st.mismatches, 0)
        self.assertTrue(st.passed())
        self.assertEqual(st.summary()["ratio"], 0.0)

    def test_none_side_skipped(self):
        st = ShadowStats()
        st.record("ema200", None, 2400.0)
        st.record("ema200", 2400.0, None)
        self.assertEqual(st.n, 0)

    def test_mismatch_above_tol_counted(self):
        st = ShadowStats()
        st.record("ema20", 100.0, 100.0)
        st.record("ema20", 100.0 * (1 + 10 * MISMATCH_TOL), 100.0)
        self.assertEqual(st.mismatches, 1)
        self.assertAlmostEqual(st.max_rel, 10 * MISMATCH_TOL, places=9)
        self.assertGreater(st.ratio, 0)

    def test_gate_ratio(self):
        st = ShadowStats()
        # 999 根一致 + 1 根超差 → 0.1% 恰好不 < 0.1% → 不通过
        for _ in range(999):
            st.record("atr14", 1.0, 1.0)
        st.record("atr14", 1.0 + 1e-5, 1.0)
        self.assertFalse(st.passed())
        # 99 根一致 + 1 根超差 = 1% 也失败；再来 2000 根一致 → 0.03% 通过
        for _ in range(1000):
            st.record("atr14", 1.0, 1.0)
        self.assertTrue(st.passed(), st.summary())

    def test_by_column_breakdown(self):
        st = ShadowStats()
        st.record_row(1, {"ema20": 1.0, "atr14": 2.0}, {"ema20": 1.0, "atr14": 2.0})
        st.record_row(2, {"ema20": 1.0, "atr14": 2.0}, {"ema20": 1.0 + 1e-3, "atr14": 2.0})
        s = st.summary()
        self.assertEqual(s["by_column"]["ema20"]["bad"], 1)
        self.assertEqual(s["by_column"]["atr14"]["bad"], 0)
        self.assertEqual(s["worst"][1], "ema20")

    def test_reset(self):
        st = ShadowStats()
        st.record("ema20", 1.0, 2.0)
        st.reset()
        self.assertEqual(st.n, 0)
        self.assertFalse(st.passed())  # n==0 时不算通过
        self.assertLess(GATE_RATIO, 1.0)


if __name__ == "__main__":
    unittest.main()
