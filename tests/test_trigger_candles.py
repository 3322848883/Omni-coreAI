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


class TestActiveTriggersVisibleToAI(unittest.TestCase):
    """AI 必须看得见自己已生效的触发器，否则无法避免重复、也无法删除旧的。

    此前生效触发器从未注入 prompt/snapshot → 5 个槽位填满后每次 add 都被拒
    （线上实测 115 次 trigger_rejected）。
    """

    def _runner(self, items):
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=["BTC_USDT"])
        r._ai_policy = type("P", (), {"max_active": 5})()

        class Store:
            def active(self_inner):
                return items

        r._ai_store = Store()
        return r

    def test_empty_when_no_triggers(self):
        self.assertEqual(self._runner([])._active_triggers_block(), "")

    def test_lists_id_type_symbol_params(self):
        t = type("T", (), {"id": "t-abc123", "type": "price_break",
                           "symbol": "BTC_USDT",
                           "params": {"lookback": 30, "side": "high"}})()
        blk = self._runner([t])._active_triggers_block()
        self.assertIn("t-abc123", blk)
        self.assertIn("price_break", blk)
        self.assertIn("lookback", blk)
        self.assertIn("1/5", blk, "应显示占用进度")

    def test_mentions_trigger_ops(self):
        t = type("T", (), {"id": "t-1", "type": "rsi", "symbol": "BTC_USDT",
                           "params": {}})()
        blk = self._runner([t])._active_triggers_block()
        self.assertIn("trigger_ops", blk)

    def test_store_error_is_silent(self):
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=["BTC_USDT"])

        class Boom:
            def active(self_inner):
                raise RuntimeError("boom")

        r._ai_store = Boom()
        self.assertEqual(r._active_triggers_block(), "")

    def test_run_once_appends_block(self):
        """端到端：run_once 真的把触发器段拼进了 user prompt。"""
        import tempfile
        from pathlib import Path
        from unittest import mock

        from omnialpha.strategist import loop as loopmod

        with tempfile.TemporaryDirectory() as td:
            r = PlanRunner(
                client=object(),
                cfg=StrategistConfig(symbols=["BTC_USDT"], vision=False,
                                     prompt_file="", bot_root=Path(td),
                                     bot_id="bot-a", env="paper"),
                inbox=Path(td) / "inbox", history_dir=Path(td) / "state",
                llm=object(),
            )
            captured = {}

            def fake_chat(system, user, chart_base64=None):
                captured["user"] = user
                return ('{"cycle_id":"c1","chips":'
                        '[{"symbol":"BTC_USDT","action":"hold","confidence":0.0}]}')

            r._chat_with_tools = fake_chat
            t = type("T", (), {"id": "t-zz9", "type": "price_break",
                               "symbol": "BTC_USDT", "params": {"lookback": 30}})()

            class Store:
                def active(self_inner):
                    return [t]

            r._ai_store = Store()
            with mock.patch.object(loopmod, "collect_snapshot", return_value={}), \
                 mock.patch.object(loopmod, "build_system_prompt", return_value="sys"), \
                 mock.patch.object(loopmod, "build_user_prompt", return_value="BASE"):
                r.run_once(trigger="test")

            self.assertIn("t-zz9", captured.get("user", ""),
                          "已生效触发器必须出现在 user prompt 里")
            self.assertIn("已生效的自设触发器", captured.get("user", ""))


class TestSystemPromptDocumentsTriggerLimits(unittest.TestCase):
    """提示词必须写明合法范围 —— 否则 AI 只能猜（线上出现过 lookback=1.0 / 3.0）。"""

    def test_ranges_documented(self):
        from omnialpha.strategist.prompt import SYSTEM_PROMPT

        for frag in ("lookback:5-300", "period:2-200", "signal:2-50",
                     "level:1-99", "mult:1-5"):
            with self.subTest(frag=frag):
                self.assertIn(frag, SYSTEM_PROMPT)

    def test_cap_and_ops_documented(self):
        from omnialpha.strategist.prompt import SYSTEM_PROMPT

        self.assertIn("最多 5 个生效", SYSTEM_PROMPT)
        self.assertIn("trigger_ops", SYSTEM_PROMPT)

    def test_allowed_types_documented(self):
        from omnialpha.strategist.prompt import SYSTEM_PROMPT

        for t in ("price_break", "rsi", "ma_cross", "macd_cross",
                  "boll_break", "atr_spike", "volume_spike", "ema_cross"):
            with self.subTest(t=t):
                self.assertIn(t, SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()

