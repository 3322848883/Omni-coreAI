# -*- coding: utf-8 -*-
"""P0.4 alerts.json 告警落盘测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate_bot.monitoring import (
    TYPE_DUP_FILL,
    TYPE_EQUITY_DEVIATION,
    TYPE_ORPHAN,
    AlertStore,
    read_alerts,
)


class TestAlertStore(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.store = AlertStore(self.root, "b1")

    def tearDown(self):
        self._td.cleanup()

    def test_file_created_on_first_alert(self):
        self.assertFalse(self.store.path.exists())
        self.store.raise_alert("test", "hello")
        self.assertTrue(self.store.path.exists())
        rec = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(len(rec["alerts"]), 1)
        self.assertEqual(rec["alerts"][0]["type"], "test")
        self.assertEqual(rec["alerts"][0]["detail"], "hello")

    def test_equity_deviation_threshold(self):
        # 偏离 5% < 10% → 不告警
        self.assertIsNone(self.store.equity_deviation(10000, 10500, threshold_pct=10.0))
        self.assertEqual(self.store.list(), [])
        # 偏离 15% > 10% → 告警
        rec = self.store.equity_deviation(10000, 8500, threshold_pct=10.0)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["type"], TYPE_EQUITY_DEVIATION)
        self.assertAlmostEqual(rec["deviation_pct"], -15.0)

    def test_equity_deviation_start_zero(self):
        self.assertIsNone(self.store.equity_deviation(0, 100))

    def test_dup_fill(self):
        rec = self.store.dup_fill("ord-1", 10.0, 83000.0)
        self.assertEqual(rec["type"], TYPE_DUP_FILL)
        self.assertEqual(rec["order_id"], "ord-1")
        rows = self.store.list(TYPE_DUP_FILL)
        self.assertEqual(len(rows), 1)

    def test_orphan(self):
        rec = self.store.orphan("BTC_USDT", 2, ["p1", "p2"])
        self.assertEqual(rec["type"], TYPE_ORPHAN)
        self.assertEqual(rec["count"], 2)
        self.assertEqual(rec["order_ids"], ["p1", "p2"])

    def test_list_filter_by_type(self):
        self.store.dup_fill("a", 1, 2)
        self.store.orphan("BTC_USDT", 1)
        self.store.equity_deviation(100, 80, threshold_pct=10.0)
        self.assertEqual(len(self.store.list()), 3)
        self.assertEqual(len(self.store.list(TYPE_DUP_FILL)), 1)
        self.assertEqual(len(self.store.list(TYPE_ORPHAN)), 1)
        self.assertEqual(len(self.store.list(TYPE_EQUITY_DEVIATION)), 1)

    def test_cap_at_max(self):
        from gate_bot.monitoring.alerts import MAX_ALERTS

        for i in range(MAX_ALERTS + 20):
            self.store.raise_alert("t", f"n{i}")
        rows = self.store.list()
        self.assertEqual(len(rows), MAX_ALERTS)
        self.assertEqual(rows[-1]["detail"], f"n{MAX_ALERTS + 19}")

    def test_corrupt_file_recovers(self):
        self.store.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.list(), [])
        self.store.raise_alert("t", "after corrupt")
        self.assertEqual(len(self.store.list()), 1)

    def test_read_alerts_helper(self):
        self.store.orphan("ETH_USDT", 1)
        rows = read_alerts(self.root, "b1", TYPE_ORPHAN)
        self.assertEqual(len(rows), 1)
        self.assertEqual(read_alerts(self.root, "other"), [])


class TestExecutorAlertHooks(unittest.TestCase):
    def test_executor_accepts_alert_store(self):
        from gate_bot.executor import Executor

        class DummyClient:
            def get_account(self):
                return {"total": "10000"}

        store = AlertStore(Path(tempfile.mkdtemp()), "b1")
        ex = Executor(DummyClient(), alert_store=store)
        self.assertIs(ex.alert_store, store)

    def test_orphan_cleanup_raises_alert(self):
        from gate_bot.executor import Executor

        class DummyClient:
            def list_price_orders(self, symbol):
                return [
                    {
                        "id": "p1",
                        "text": "t-bb-sl",
                        "reduce_only": True,
                        "status": "open",
                        "initial": {"text": "t-bb-sl", "reduce_only": True},
                    }
                ]

            def get_positions(self):
                return []  # 无持仓 → 是孤儿

            def cancel_price_order(self, pid):
                return True

        tmp = Path(tempfile.mkdtemp())
        store = AlertStore(tmp, "b1")
        ex = Executor(DummyClient(), label_prefix="bb", alert_store=store)
        cancelled = ex._cleanup_orphan_protectors("BTC_USDT")
        self.assertEqual(cancelled, ["p1"])
        alerts = store.list(TYPE_ORPHAN)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()

class TestOrphanFieldName(unittest.TestCase):
    """Gate 返回 is_reduce_only —— 字段名不匹配会导致孤儿永不被识别。"""

    def _ex(self):
        from gate_bot.executor import Executor

        class DummyClient:
            def list_price_orders(self, symbol):
                return []
            def get_positions(self):
                return []
            def cancel_price_order(self, pid):
                return True
            def get_account(self):
                return {"total": "10000"}

        return Executor(DummyClient(), label_prefix="brk")

    def test_is_reduce_only_field_from_gate(self):
        """Gate API 用 initial.is_reduce_only。"""
        ex = self._ex()
        po = {
            "id": "p1",
            "text": "t-brk-tp",
            "status": "open",
            "initial": {"text": "t-brk-tp", "is_reduce_only": True, "size": -10},
        }
        self.assertTrue(ex._order_is_reduce_only(po))
        self.assertTrue(ex._is_orphan_protector(po, []))  # 无持仓 → 孤儿

    def test_reduce_only_field_from_paper(self):
        """paper 侧用 reduce_only。"""
        ex = self._ex()
        po = {
            "id": "p1",
            "text": "t-brk-sl",
            "status": "open",
            "reduce_only": True,
            "initial": {"text": "t-brk-sl", "reduce_only": True, "size": -10},
        }
        self.assertTrue(ex._order_is_reduce_only(po))
        self.assertTrue(ex._is_orphan_protector(po, []))

    def test_non_reduce_only_not_orphan(self):
        ex = self._ex()
        po = {
            "id": "p1",
            "text": "t-brk-entry",
            "status": "open",
            "initial": {"text": "t-brk-entry", "is_reduce_only": False, "size": 10},
        }
        self.assertFalse(ex._order_is_reduce_only(po))
        self.assertFalse(ex._is_orphan_protector(po, []))

    def test_protector_with_position_not_orphan(self):
        ex = self._ex()
        po = {
            "id": "p1",
            "text": "t-brk-tp",
            "status": "open",
            "initial": {"text": "t-brk-tp", "is_reduce_only": True, "size": -10},
        }
        positions = [{"contract": "BTC_USDT", "size": 5}]  # 有多仓 → 卖平不是孤儿
        self.assertFalse(ex._is_orphan_protector(po, positions))


class TestNotifySwitch(unittest.TestCase):
    """模拟盘/实盘通知开关：模拟盘默认关。"""

    def test_paper_default_off(self):
        from gate_bot.monitoring import should_notify

        with tempfile.TemporaryDirectory() as td:
            # 无配置 → 实盘开、模拟盘关（安全默认）
            self.assertTrue(should_notify(Path(td), "live"))
            self.assertFalse(should_notify(Path(td), "paper"))

    def test_yaml_switch(self):
        from gate_bot.monitoring import should_notify

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config").mkdir()
            (Path(td) / "config" / "alerts.yaml").write_text(
                "notify:\n  live: true\n  paper: true\n", encoding="utf-8"
            )
            self.assertTrue(should_notify(Path(td), "live"))
            self.assertTrue(should_notify(Path(td), "paper"))

    def test_live_can_be_disabled(self):
        from gate_bot.monitoring import should_notify

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config").mkdir()
            (Path(td) / "config" / "alerts.yaml").write_text(
                "notify:\n  live: false\n  paper: false\n", encoding="utf-8"
            )
            self.assertFalse(should_notify(Path(td), "live"))

    def test_trade_events_gated_by_env(self):
        from gate_bot.monitoring import notify_trade_events

        steps = [{"action": "open_long", "ok": True, "symbol": "BTC_USDT",
                  "detail": {"price": 83000, "sl": 82800, "tp": 83500, "size_usd": 100}}]
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("gate_bot.monitoring.notify_process_event") as _:
                # paper 默认关 → 不发
                with mock.patch("gate_bot.monitoring.notify.build_notifier") as bn:
                    notify_trade_events("bot-x", steps, root=Path(td), env="paper")
                    bn.assert_not_called()
                # live → 会尝试发
                with mock.patch("gate_bot.monitoring.notify.build_notifier") as bn:
                    bn.return_value.has_channel = False
                    notify_trade_events("bot-x", steps, root=Path(td), env="live")
                    bn.assert_called()

    def test_open_card_has_tp_sl(self):
        """开仓卡片必须含止损/止盈（不能是空模板）。"""
        from gate_bot.monitoring import format_trade_card

        steps = [{
            "action": "open_short", "ok": True, "symbol": "BTC_USDT",
            "detail": {
                "price": 83350.0, "size_usd": 24373.295400837662,
                "sl": 83720.0, "tp": 82560.0,
            },
        }]
        cards = format_trade_card("brooks-btc", steps)
        self.assertEqual(len(cards), 1)
        text = str(cards[0])
        self.assertIn("83350", text)   # 入场价
        self.assertIn("83720", text)   # 止损
        self.assertIn("82560", text)   # 止盈
        self.assertIn("24373", text)   # 仓位（已格式化）

    def test_open_card_no_empty_fields(self):
        """关键字段不能是空字符串。"""
        from gate_bot.monitoring import format_trade_card

        steps = [{
            "action": "open_long", "ok": True, "symbol": "BTC_USDT",
            "detail": {"entry_price": 83000, "sl": 82800, "tp": 83500, "size_usd": 100},
        }]
        cards = format_trade_card("bb", steps)
        elems = str(cards[0])
        # 关键数值必须在卡片里
        self.assertIn("82800", elems)
        self.assertIn("82800", elems)
        self.assertIn("83500", elems)
