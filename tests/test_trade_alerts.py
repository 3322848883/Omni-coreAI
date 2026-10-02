# -*- coding: utf-8 -*-
"""成交告警模板与接线测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.monitoring import format_trade_steps, notify_trade_events
from omnialpha.monitoring.notify import build_notifier


class TestFormatTradeSteps(unittest.TestCase):
    def test_open_close_reduce_modify(self):
        steps = [
            {"action": "open_long", "ok": True, "symbol": "BTC_USDT",
             "detail": {"price": 83000, "size_usd": 585}},
            {"action": "close", "ok": True, "symbol": "BTC_USDT",
             "detail": {"price": 83500, "realized_pnl": 1.2}},
            {"action": "reduce_long", "ok": True, "symbol": "BTC_USDT",
             "detail": {"price": 83200, "size": 17}},
            {"action": "modify_tp_sl", "ok": True, "symbol": "BTC_USDT",
             "detail": {"tp": 82560, "sl": 83720}},
        ]
        lines = format_trade_steps("bb", steps)
        self.assertEqual(len(lines), 4)
        self.assertIn("开仓 BTC_USDT 多", lines[0])
        self.assertIn("83000", lines[0])
        self.assertIn("止盈", lines[1])
        self.assertIn("pnl=1.2", lines[1])
        self.assertIn("减仓 BTC_USDT", lines[2])
        self.assertIn("改保护 BTC_USDT", lines[3])
        self.assertIn("TP=82560", lines[3])
        self.assertIn("SL=83720", lines[3])

    def test_short_side_labeled(self):
        lines = format_trade_steps("bb", [
            {"action": "open_short", "ok": True, "symbol": "BTC_USDT",
             "detail": {"price": 83380, "size_usd": 585}},
        ])
        self.assertIn("开仓 BTC_USDT 空", lines[0])

    def test_hold_cancel_silent(self):
        steps = [
            {"action": "hold", "ok": True, "symbol": "BTC_USDT", "detail": {}},
            {"action": "cancel_all", "ok": True, "symbol": "BTC_USDT", "detail": {}},
            {"action": "cancel_price_all", "ok": True, "symbol": "BTC_USDT", "detail": {}},
        ]
        self.assertEqual(format_trade_steps("bb", steps), [])

    def test_failed_step_marked(self):
        lines = format_trade_steps("bb", [
            {"action": "open_long", "ok": False, "symbol": "BTC_USDT",
             "detail": {"price": 83000}},
        ])
        self.assertIn("✗", lines[0])

    def test_empty_and_bad_input(self):
        self.assertEqual(format_trade_steps("bb", []), [])
        self.assertEqual(format_trade_steps("bb", None), [])
        self.assertEqual(format_trade_steps("bb", ["not-a-dict"]), [])
        self.assertEqual(format_trade_steps("bb", [{"action": "hold"}]), [])


class TestBuildNotifier(unittest.TestCase):
    def test_no_env_no_channel(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            n = build_notifier(root=Path("/nonexistent"))
            self.assertFalse(n.has_channel)

    def test_feishu_webhook(self):
        with mock.patch.dict("os.environ", {"FEISHU_WEBHOOK": "https://x"}, clear=True):
            n = build_notifier(root=Path("/nonexistent"))
            self.assertTrue(n.has_channel)
            self.assertEqual(n.channel_names, ["FeishuChannel"])

    def test_telegram(self):
        env = {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        with mock.patch.dict("os.environ", env, clear=True):
            n = build_notifier(root=Path("/nonexistent"))
            self.assertTrue(n.has_channel)
            self.assertEqual(n.channel_names, ["TelegramChannel"])


class TestNotifyTradeEvents(unittest.TestCase):
    def test_no_channel_returns_false(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(notify_trade_events("bb", [
                {"action": "open_long", "ok": True, "symbol": "BTC_USDT",
                 "detail": {"price": 1, "size_usd": 2}},
            ], root=Path("/nonexistent")))

    def test_hold_only_no_send(self):
        with mock.patch.dict("os.environ", {"FEISHU_WEBHOOK": "https://x"}, clear=True):
            # hold 不推送 → 即使有渠道也不应调 send
            with mock.patch.object(
                __import__("omnialpha.monitoring.notify", fromlist=["AlertNotifier"]),
                "AlertNotifier"
            ) as MockN:
                notify_trade_events("bb", [{"action": "hold", "ok": True}])
                MockN.assert_not_called()

    def test_trade_sends(self):
        env = {"FEISHU_WEBHOOK": "https://x"}
        with mock.patch.dict("os.environ", env, clear=True):
            sent = []
            with mock.patch(
                "omnialpha.monitoring.notify.AlertNotifier.send_card",
                lambda self, card: sent.append(card) or True,
            ):
                ok = notify_trade_events("bb", [
                    {"action": "open_short", "ok": True, "symbol": "BTC_USDT",
                     "detail": {"price": 83380, "size_usd": 585}},
                ])
            self.assertTrue(ok)
            self.assertEqual(len(sent), 1)
            self.assertIsInstance(sent[0], dict)
            self.assertIsInstance(sent[0], dict)


class TestTradeLogHooks(unittest.TestCase):
    def test_log_execution_calls_notify(self):
        from omnialpha.tradelog import TradeLogger

        with tempfile.TemporaryDirectory() as td:
            log = TradeLogger(Path(td) / "trades.jsonl")
            with mock.patch(
                "omnialpha.monitoring.notify_trade_events"
            ) as m:
                log.log_execution(
                    "bb",
                    {"plan_cycle": "c1"},
                    {"ok": True, "steps": [{"action": "open_long", "ok": True,
                                           "symbol": "BTC_USDT", "detail": {}}]},
                )
                m.assert_called_once()
                self.assertEqual(m.call_args[0][0], "bb")


if __name__ == "__main__":
    unittest.main()
