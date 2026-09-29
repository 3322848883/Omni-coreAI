# -*- coding: utf-8 -*-
"""vol_adjust_size 挂钩 executor 测试。"""
from __future__ import annotations

import unittest

from gate_bot.executor import Executor
from gate_bot.sizing import vol_adjust_size


class DummyClient:
    def __init__(self, klines=None, account=None):
        self._klines = klines or []
        self._account = account or {"total": "10000"}

    def get_klines(self, symbol, interval, limit):
        return self._klines

    def get_account(self):
        return self._account


def _klines(atr_target_pct: float, close: float = 100.0, n: int = 15):
    """构造恒定 TR 的 K 线，使 ATR% ≈ atr_target_pct。"""
    tr = close * atr_target_pct / 100.0
    rows = []
    for i in range(n):
        rows.append({
            "t": 1700000000 + i * 3600,
            "o": close, "h": close + tr, "l": close, "c": close,
        })
    return rows


class TestVolAdjust(unittest.TestCase):
    def test_sizing_math(self):
        # atr 2% target 2% → 倍数 1
        self.assertEqual(vol_adjust_size(1000, 2.0, 2.0), 1000)
        # atr 4% target 2% → 倍数 0.5
        self.assertEqual(vol_adjust_size(1000, 4.0, 2.0), 500)
        # atr 1% target 2% → 倍数 2（上限）
        self.assertEqual(vol_adjust_size(1000, 1.0, 2.0), 2000)

    def test_disabled_when_no_config(self):
        ex = Executor(DummyClient(), account_risk={})
        self.assertEqual(ex._vol_adjust(_Intent(), 1000), (1000, ""))
        ex2 = Executor(DummyClient(), account_risk={"vol_target_pct": 0})
        self.assertEqual(ex2._vol_adjust(_Intent(), 1000), (1000, ""))

    def test_adjusts_with_atr(self):
        kl = _klines(4.0)  # ATR≈4%
        ex = Executor(
            DummyClient(klines=kl),
            account_risk={"vol_target_pct": 2.0},
        )
        size, note = ex._vol_adjust(_Intent(symbol="BTC_USDT"), 1000)
        self.assertEqual(size, 500)
        self.assertIn("vol_adjust", note)
        self.assertIn("1000->500", note)

    def test_no_adjust_when_atr_missing(self):
        ex = Executor(
            DummyClient(klines=[]),
            account_risk={"vol_target_pct": 2.0},
        )
        self.assertEqual(ex._vol_adjust(_Intent(symbol="BTC_USDT"), 1000), (1000, ""))

    def test_atr_pct_computed(self):
        kl = _klines(4.0, close=100.0)
        ex = Executor(DummyClient(klines=kl))
        self.assertAlmostEqual(ex._atr_pct("BTC_USDT"), 4.0, places=1)


class _Intent:
    def __init__(self, symbol="BTC_USDT"):
        self.symbol = symbol
        self.price = None
        self.size_usd = None
        self.size = None
        self.size_pct = None
        self.margin_pct = None
        self.sl = None
        self.tp = None
        self.tp2 = None
        self.leverage = None
        self.margin_mode = None
        self.action = "open_long"
        self.meta = {}


if __name__ == "__main__":
    unittest.main()
