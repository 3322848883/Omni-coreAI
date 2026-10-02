# -*- coding: utf-8 -*-
"""条件/事件触发取数回归：客户端重构后 public_get 消失导致的「永不触发」。

线上实测（2026-10-02，实盘 brooks-btc）：
  178 个周期全部 'trigger': 'interval'，'cond[' 出现 0 次 —— AI 自设触发器与
  kline_close 事件从未触发过。
根因：triggers._load_candles 与 PlanRunner._kline_closed 直接调
  client.public_get()，而客户端重构成 ExchangeClient(GateExchange) 后没有该方法
  → 每次求值抛 AttributeError → 被兜底成 False → 永远不触发。

修法：改走 market.fetch_rest_candles()（有 get_klines 用 get_klines，
否则回退 public_get），新旧客户端都兼容。
"""
from __future__ import annotations

import unittest

from omnialpha.strategist.loop import PlanRunner, StrategistConfig
from omnialpha.strategist.triggers import evaluate_condition


class ModernClient:
    """模拟重构后的 ExchangeClient：有 get_klines，**故意不定义 public_get**。"""

    def __init__(self, closes: list[float]):
        self.closes = closes

    def get_klines(self, symbol, interval, limit=100):
        rows = self.closes[-int(limit):]
        return [
            {"t": 1700000000 + i * 900, "o": c, "h": c + 10.0, "l": c - 10.0,
             "c": c, "v": 1.0}
            for i, c in enumerate(rows)
        ]


class LegacyClient:
    """旧式客户端：只有 public_get（Gate 的 [t,v,c,h,l,o] 列表格式）。"""

    def __init__(self, closes: list[float]):
        self.closes = closes

    def public_get(self, path, query_string=""):
        return [
            [1700000000 + i * 900, "1", str(c), str(c + 10.0), str(c - 10.0), str(c)]
            for i, c in enumerate(self.closes)
        ]


PB_HIGH = {"type": "price_break", "symbol": "BTC_USDT", "lookback": 30, "side": "high"}


class TestEvaluateConditionCandles(unittest.TestCase):
    def test_fires_with_modern_client(self):
        """最后一根突破窗口高 → 应触发（此前因 public_get 缺失恒为 False）。"""
        c = ModernClient([100.0] * 40 + [200.0])
        ok, reason = evaluate_condition(c, PB_HIGH, "15m")
        self.assertNotIn("candles error", reason, f"取数仍失败: {reason}")
        self.assertTrue(ok, reason)

    def test_not_fired_when_no_break(self):
        c = ModernClient([100.0] * 41)
        ok, reason = evaluate_condition(c, PB_HIGH, "15m")
        self.assertNotIn("candles error", reason)
        self.assertFalse(ok, reason)

    def test_low_side_break(self):
        c = ModernClient([100.0] * 40 + [50.0])
        ok, reason = evaluate_condition(
            c, {**PB_HIGH, "side": "low"}, "15m")
        self.assertNotIn("candles error", reason)
        self.assertTrue(ok, reason)

    def test_legacy_client_still_supported(self):
        c = LegacyClient([100.0] * 40 + [200.0])
        ok, reason = evaluate_condition(c, PB_HIGH, "15m")
        self.assertNotIn("candles error", reason)
        self.assertTrue(ok, reason)

    def test_reason_is_not_attribute_error(self):
        """回归断言：不能再出现 AttributeError 被兜底。"""
        c = ModernClient([100.0] * 41)
        _, reason = evaluate_condition(c, PB_HIGH, "15m")
        self.assertNotIn("public_get", reason)
        self.assertNotIn("AttributeError", reason)


class TestKlineClosed(unittest.TestCase):
    def _runner(self, client, n=0) -> PlanRunner:
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=["BTC_USDT"], timeframe="15m",
                                 event_timeframe="15m")
        r.client = client
        r._last_kline_t = n or None
        return r

    def test_first_call_is_false_then_advance_is_true(self):
        c = ModernClient([100.0] * 41)
        r = self._runner(c)
        self.assertFalse(r._kline_closed(), "首次只记录基线，不应算收盘")
        # 时间戳前进 → 算收盘
        r._last_kline_t = r._last_kline_t - 900
        self.assertTrue(r._kline_closed())

    def test_same_timestamp_is_false(self):
        c = ModernClient([100.0] * 41)
        r = self._runner(c)
        r._kline_closed()          # 记录基线
        self.assertFalse(r._kline_closed(), "同一根 K 线不应重复触发")

    def test_works_without_public_get(self):
        """关键：客户端没有 public_get 时也必须能工作（此前恒 False）。"""
        c = ModernClient([100.0] * 41)
        self.assertFalse(hasattr(c, "public_get"))
        r = self._runner(c, n=1)
        self.assertIsInstance(r._kline_closed(), bool)


if __name__ == "__main__":
    unittest.main()
