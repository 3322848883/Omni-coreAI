# -*- coding: utf-8 -*-
"""T16：per-venue 覆盖矩阵 —— 「这个币这家所有没有」与「数据取没取到」是两件事。

前者是上币差异（换所或换币就行），后者是要修的故障。混在一起看会把前者当故障排查、
把后者当正常放过。矩阵只回答「有没有」，且**三态**：`True`/`False` 是测出来的，
`None` 是**不知道**（该所没接列合约校验）—— 不把「不知道」冒充成「有」或「没有」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.exchanges import coverage  # noqa: E402
from omnialpha.exchanges.registry import list_exchanges  # noqa: E402


class TestCoverageMatrix(unittest.TestCase):
    def test_known_venues_are_all_covered(self):
        """注册表里的每家所都要在矩阵里出现（新增一家所时别漏）。"""
        self.assertEqual(sorted(coverage.venues()), sorted(list_exchanges()))

    def test_unverified_is_none_not_false(self):
        """没接入列合约校验的所 → `None`（不知道），**不是** `False`（没有）。"""
        m = coverage.coverage_matrix(["BTC_USDT"])
        for v in coverage.venues():
            if coverage.supports_listing(v):
                continue
            self.assertIsNone(m["BTC_USDT"][v], v)

    def test_measured_sets_give_true_and_false(self):
        m = coverage.coverage_matrix(
            ["BTC_USDT", "PEPE_USDT"],
            venue_symbols={"binance": ["BTC_USDT"], "gate": ["BTC_USDT", "PEPE_USDT"]})
        self.assertTrue(m["PEPE_USDT"]["gate"])
        self.assertFalse(m["PEPE_USDT"]["binance"], "binance 无 PEPE（上币差异）")
        self.assertEqual(coverage.gaps(m), {"PEPE_USDT": ["binance"]})

    def test_unverified_helper_does_not_confuse_with_gaps(self):
        m = coverage.coverage_matrix(["BTC_USDT"],
                                     venue_symbols={"gate": ["BTC_USDT"]})
        self.assertEqual(coverage.gaps(m), {}, "gate 有 BTC → 不是 gap")
        self.assertIn("binance", coverage.unverified(m)["BTC_USDT"])

    def test_normalises_case_and_skips_blank(self):
        m = coverage.coverage_matrix([" btc_usdt ", "", None],
                                     venue_symbols={"gate": ["BTC_USDT"]})
        self.assertEqual(list(m), ["BTC_USDT"])

    def test_empty_inputs_are_safe(self):
        self.assertEqual(coverage.coverage_matrix([]), {})
        self.assertEqual(coverage.gaps({}), {})
        self.assertEqual(coverage.unverified({}), {})
        self.assertFalse(coverage.supports_listing("nope"))


if __name__ == "__main__":
    unittest.main()
