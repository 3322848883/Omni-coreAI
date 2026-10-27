# -*- coding: utf-8 -*-
"""Bollinger Bands（Pine v6 BB 脚本）与工具实现的逐位对拍。

Pine 语义（用户提供的官方 v6 BB）：
    basis = ta.sma(src, 20)              # 默认 SMA
    dev   = mult * ta.stdev(src, 20)     # ta.stdev = **总体**标准差
    upper = basis + dev
    lower = basis - dev
对应实现：omnialpha/strategist/indicators.py 的 boll()（SMA 默认、总体 stdev），
以及指标名解析 boll20 / boll20_K / boll20_ema / boll_ema_upper 等全部变体。
"""
import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.indicators import (attach_indicators,  # noqa: E402
                                            parse_indicator_name)


def pine_bb(closes, length=20, mult=2.0):
    """按 Pine v6 BB 脚本逐步实现（独立参照，不 import 实现代码）。"""
    n = len(closes)
    basis = [None] * n
    upper = [None] * n
    lower = [None] * n
    for i in range(length - 1, n):
        w = closes[i - length + 1: i + 1]
        m = sum(w) / length
        sd = (sum((x - m) ** 2 for x in w) / length) ** 0.5
        basis[i] = m
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return basis, upper, lower


def bars(n=600, seed=13):
    rnd = random.Random(seed)
    px = 2400.0
    rows = []
    for i in range(n):
        o = px
        c = max(1.0, o + rnd.gauss(0, 1.4))
        rows.append({"t": 1_700_000_000 + i * 3600, "o": o,
                     "h": max(o, c) + 0.6, "l": min(o, c) - 0.6, "c": c, "v": 10.0})
        px = c
    return rows


class TestPineParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.closes = [r["c"] for r in bars()]
        cls.rows = attach_indicators(bars(), ["boll20", "boll_upper", "boll_lower",
                                              "boll20_2.5", "boll20_ema_upper"])

    def _close(self, a, b):
        self.assertTrue(a is not None and b is not None)
        self.assertTrue(math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12),
                        f"{a} != {b}")

    def test_basis_matches_pine_sma(self):
        basis, _, _ = pine_bb(self.closes, 20, 2.0)
        for i in range(19, len(self.closes)):
            self._close(self.rows[i]["boll20"], basis[i])

    def test_upper_lower_match_pine(self):
        _, upper, lower = pine_bb(self.closes, 20, 2.0)
        for i in range(19, len(self.closes)):
            self._close(self.rows[i]["boll_upper"], upper[i])
            self._close(self.rows[i]["boll_lower"], lower[i])

    def test_custom_mult(self):
        # bot 侧自定义倍数暴露为 `boll20_2.5_upper/_lower`（数据源落库为 boll20_2p5_*）
        basis, upper, _ = pine_bb(self.closes, 20, 2.5)
        for i in range(19, len(self.closes)):
            self._close(self.rows[i].get("boll20_2.5_upper"), upper[i])

    def test_warmup_is_none(self):
        self.assertIsNone(self.rows[5]["boll20"])
        self.assertIsNone(self.rows[5]["boll_upper"])


class TestToolPath(unittest.TestCase):
    """端到端：indicators 工具按预热取数并返回 boll 值（非 None）。"""

    def test_tool_returns_boll(self):
        from omnialpha.strategist.market import MarketConfig
        from omnialpha.strategist.tools import run_tool

        class FakeClient:
            def get_klines(self, symbol, interval, limit):
                rows = bars(max(limit, 60))
                return [{"t": r["t"], "v": r["v"], "c": r["c"], "h": r["h"],
                         "l": r["l"], "o": r["o"], "sum": 0.0} for r in rows]

        cfg = MarketConfig(mode="rest_only", indicators=["boll20"])
        out = run_tool(FakeClient(), "indicators",
                       {"symbol": "ETH_USDT", "tf": "1h",
                        "names": ["boll20", "boll_upper", "boll_lower"]},
                       market_cfg=cfg)
        latest = out.get("latest") or {}
        self.assertIsNotNone(latest.get("boll20"))
        self.assertIsNotNone(latest.get("boll_upper"))
        self.assertIsNotNone(latest.get("boll_lower"))
        self.assertGreater(float(latest["boll_upper"]), float(latest["boll_lower"]))


class TestNameVariants(unittest.TestCase):
    def test_supported_forms(self):
        for name, expect_kind in (("boll", "boll"), ("boll20", "boll"),
                                  ("boll20_2.5", "boll"), ("boll20_ema", "boll"),
                                  ("boll_ema_upper", "boll"), ("boll20_ema_upper", "boll"),
                                  ("boll_upper", "boll"), ("boll_middle", "boll"),
                                  ("boll_lower", "boll")):
            spec = parse_indicator_name(name)
            self.assertEqual(spec["kind"], expect_kind, name)

    def test_rejected_forms(self):
        for name in ("bb20", "bbands", "BB20", "boll_"):
            with self.assertRaises(Exception, msg=name):
                parse_indicator_name(name)


if __name__ == "__main__":
    unittest.main()
