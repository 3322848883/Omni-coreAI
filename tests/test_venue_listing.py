# -*- coding: utf-8 -*-
"""T16：Bitget / Hyperliquid 的**真实列合约**（per-venue 覆盖矩阵的实测来源）。

覆盖矩阵（`omnialpha/exchanges/coverage.py`）本身不发请求，它要调用方提供**实测集合**。
这里补的就是这两家所的实测来源 —— 公开端点、无需密钥：

- Bitget：`GET /api/v2/mix/market/contracts?productType=USDT-FUTURES`
- Hyperliquid：`POST /info {"type":"meta"}` → `universe[].name`

**三态**：拿到 → `set`；该所没有这个币 → 不在 set 里（调用方据此判「确认没有」）；
**取不到 → `None`**（= 未校验）。把「取不到」当成空集就是拿「不知道」当「没有」，
后果是把一个真实存在的币判成上币差异而放过（spec §S2.4⑪）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.exchanges.bitget import BitgetExchange, BitgetMapper  # noqa: E402
from omnialpha.exchanges.hyperliquid import HyperliquidExchange  # noqa: E402


class TestBitgetAvailableSymbols(unittest.TestCase):
    def _ex(self, payload):
        ex = BitgetExchange(env="live", api_key="k", api_secret="s")
        ex._req = lambda *a, **kw: payload  # type: ignore[method-assign]
        return ex

    def test_reads_real_listing(self):
        ex = self._ex({"data": [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"},
                                {"symbol": "1000PEPEUSDT"}]})
        self.assertEqual(ex.available_symbols(),
                         {"BTC_USDT", "ETH_USDT", "1000PEPE_USDT"})
        self.assertEqual(ex.listing_note, "")

    def test_api_error_returns_none_not_empty_set(self):
        ex = BitgetExchange(env="live", api_key="k", api_secret="s")

        def boom(*a, **kw):
            raise RuntimeError("network down")

        ex._req = boom  # type: ignore[method-assign]
        self.assertIsNone(ex.available_symbols(), "取不到 ≠ 没有")
        self.assertIn("network down", ex.listing_note)

    def test_empty_payload_returns_none(self):
        ex = self._ex({"data": []})
        self.assertIsNone(ex.available_symbols())
        self.assertTrue(ex.listing_note)

    def test_internal_names_round_trip(self):
        m = BitgetMapper()
        self.assertEqual(m.internal(m.native("BTC_USDT")), "BTC_USDT")


class TestHyperliquidAvailableSymbols(unittest.TestCase):
    def _ex(self, payload):
        ex = HyperliquidExchange(env="live", api_key="k", api_secret="s")
        ex._info = lambda *a, **kw: payload  # type: ignore[method-assign]
        return ex

    def test_reads_meta_universe(self):
        ex = self._ex({"universe": [{"name": "BTC"}, {"name": "DOGE"},
                                    {"name": "PURR", "isDelisted": True}]})
        self.assertEqual(ex.available_symbols(), {"BTC_USDT", "DOGE_USDT"})
        self.assertEqual(ex.listing_note, "")

    def test_api_error_returns_none_not_empty_set(self):
        ex = HyperliquidExchange(env="live", api_key="k", api_secret="s")

        def boom(*a, **kw):
            raise RuntimeError("tls reset")

        ex._info = boom  # type: ignore[method-assign]
        self.assertIsNone(ex.available_symbols(), "取不到 ≠ 没有")
        self.assertIn("tls reset", ex.listing_note)

    def test_empty_universe_returns_none(self):
        ex = self._ex({"universe": []})
        self.assertIsNone(ex.available_symbols())
        self.assertTrue(ex.listing_note)


class TestCoverageMatrixIntegration(unittest.TestCase):
    """实测集合喂给覆盖矩阵：能分出「确认没有」与「未校验」。"""

    def test_matrix_from_real_listing(self):
        from omnialpha.exchanges.coverage import coverage_matrix, gaps, unverified

        matrix = coverage_matrix(
            ["BTC_USDT", "XAU_USDT"],
            venue_symbols={"bitget": {"BTC_USDT"}, "hyperliquid": {"BTC_USDT"}})
        self.assertEqual(matrix["BTC_USDT"]["bitget"], True)
        self.assertEqual(matrix["XAU_USDT"]["bitget"], False)
        self.assertEqual(matrix["BTC_USDT"]["binance"], None, "未提供实测集合 = 不知道")
        self.assertEqual(gaps(matrix), {"XAU_USDT": ["bitget", "hyperliquid"]})
        self.assertEqual(set(unverified(matrix)["BTC_USDT"]),
                         {"gate", "binance", "okx", "bybit"})


if __name__ == "__main__":
    unittest.main()
