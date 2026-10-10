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


class TestFormatRows(unittest.TestCase):
    """矩阵的**渲染**也要与 CLI/deploy-check 共用一份（别两处各写一套，会漂移）。"""

    def test_three_states_are_distinguishable(self):
        m = coverage.coverage_matrix(["BTC_USDT"], venue_symbols={"gate": ["BTC_USDT"]})
        line = coverage.format_rows(m)[0]
        self.assertTrue(line.startswith("BTC_USDT: "), line)
        self.assertIn("gate=有", line)
        self.assertIn("=?", line, "未校验的所要标 ?（不知道 ≠ 没有）")

    def test_missing_is_renderable(self):
        m = coverage.coverage_matrix(["PEPE_USDT"], venue_symbols={"gate": ["BTC_USDT"]})
        self.assertIn("gate=没有", coverage.format_rows(m)[0])

    def test_empty_matrix_renders_nothing(self):
        self.assertEqual(coverage.format_rows({}), [])


class TestVenueListings(unittest.TestCase):
    """`venue_listings`：尽力而为地取各所的实测清单 —— 取不到就**不进入返回**。

    为什么不返回空集：`coverage_matrix` 把空集判成「确认没有」，那会把"取不到"
    变成"上币差异"这个**结论**（正是 T16 要防的那类静默失真）。
    """

    def test_gate_failure_is_omitted(self):
        from unittest import mock

        with mock.patch("omnialpha.config.fetch_exchange_contracts", return_value=None):
            self.assertEqual(coverage.venue_listings(["gate"]), {})

    def test_gate_success_is_kept(self):
        from unittest import mock

        with mock.patch("omnialpha.config.fetch_exchange_contracts",
                        return_value={"BTC_USDT"}):
            self.assertEqual(coverage.venue_listings(["gate"]), {"gate": {"BTC_USDT"}})

    def test_adapter_available_symbols_is_used(self):
        from unittest import mock

        from omnialpha.exchanges.bitget import BitgetExchange

        with mock.patch.object(BitgetExchange, "available_symbols",
                               return_value={"BTC_USDT", "ETH_USDT"}):
            out = coverage.venue_listings(["bitget"])
        self.assertEqual(out, {"bitget": {"BTC_USDT", "ETH_USDT"}})

    def test_venue_without_capability_is_omitted(self):
        """没有 `available_symbols` 的所 → 不进入返回（矩阵标 ?，而不是"没有"）。"""
        self.assertEqual(coverage.venue_listings(["binance"]), {})

    def test_never_raises(self):
        from unittest import mock

        from omnialpha.exchanges.bitget import BitgetExchange

        with mock.patch.object(BitgetExchange, "available_symbols",
                               side_effect=RuntimeError("network down")):
            self.assertEqual(coverage.venue_listings(["bitget"]), {})


if __name__ == "__main__":
    unittest.main()
