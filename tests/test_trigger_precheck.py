# -*- coding: utf-8 -*-
"""TP/SL 触发价预检 + exits 失败回滚的单元测试。

背景：Gate 条件单要求 rule=1（涨破触发）的触发价 > mark、rule=2（跌破触发）的
触发价 < mark，否则直接拒（AUTO_TRIGGER_PRICE_GREATE_MARK / _LESS_MARK）。
而 TP/SL 价位是从**入场价**推出来的：入场价离市价较远时，推出来的价位落在 mark
的非法一侧 —— 那时保护单挂不上，但入场单已经挂在交易所上了，等于留下一张
**无保护的待成交委托**（一旦成交就是裸仓）。

实测（2026-10-01/02 实盘）：`sl#2 ... AUTO_TRIGGER_PRICE_LESS_MARK`（计划入场
84900、SL 84560，但 mark 已跌破 84560）、`tp1#2 ... AUTO_TRIGGER_PRICE_GREATE_MARK`。

回滚策略：未成交 → 撤单；**已成交 → 只告警，不自动平仓**（自动平仓路径未经实盘
验证，误判代价是主动扔掉策略想持有的仓位，爆炸半径过大）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor
from omnialpha.monitoring import AlertStore, read_alerts
from omnialpha.schema import Intent


class TriggerClient:
    """只实现预检/回滚用到的方法。"""

    def __init__(self, mark: float = 86000.0, ticker_error: bool = False):
        self.mark = mark
        self.ticker_error = ticker_error
        self.cancelled = []
        self.closed = []

    def get_ticker(self, symbol):
        if self.ticker_error:
            raise RuntimeError("no ticker")
        return {"last": self.mark, "mark_price": self.mark}

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))
        return {"id": order_id}

    def close_position(self, contract, side=None, size=0):
        self.closed.append((contract, side, size))
        return {"id": "close-x", "status": "finished"}


def make_intent(action: str = "open_long", **kw) -> Intent:
    base = dict(symbol="BTC_USDT", trigger_rule_tp=1, trigger_rule_sl=2,
                tp_mode="trigger", sl_mode="trigger")
    base.update(kw)
    return Intent(action=action, **base)


class TestTriggerPrecheck(unittest.TestCase):
    def _ex(self, client):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Executor(client, label_prefix="pt", root=Path(td.name), bot_id="t")

    def test_long_tp_above_and_sl_below_mark_ok(self):
        """多头常态：TP 在 mark 上方、SL 在下方 → 放行。"""
        ex = self._ex(TriggerClient(mark=86000.0))
        self.assertIsNone(ex._precheck_exit_triggers(make_intent(tp=86900.0, sl=85300.0)))

    def test_long_sl_above_mark_blocked(self):
        """多头 SL 是 rule=2（跌破），必须在 mark 下方；在上方会被交易所拒。"""
        ex = self._ex(TriggerClient(mark=84000.0))
        err = ex._precheck_exit_triggers(make_intent(tp=85000.0, sl=84560.0))
        self.assertIsNotNone(err)
        self.assertIn("TRIGGER_PRICE_SIDE", err)
        self.assertIn("sl=84560", err)

    def test_long_tp_below_mark_blocked(self):
        """多头 TP 是 rule=1（涨破），必须在 mark 上方。"""
        ex = self._ex(TriggerClient(mark=86000.0))
        err = ex._precheck_exit_triggers(make_intent(tp=85500.0, sl=85000.0))
        self.assertIsNotNone(err)
        self.assertIn("tp=85500", err)

    def test_short_rules_inverted(self):
        """空头对调：TP rule=2（跌破，在下方）、SL rule=1（涨破，在上方）。"""
        ex = self._ex(TriggerClient(mark=86000.0))
        ok = make_intent(action="open_short", tp=85000.0, sl=87000.0,
                         trigger_rule_tp=2, trigger_rule_sl=1)
        self.assertIsNone(ex._precheck_exit_triggers(ok))
        bad = make_intent(action="open_short", tp=87000.0, sl=85000.0,
                          trigger_rule_tp=2, trigger_rule_sl=1)
        self.assertIsNotNone(ex._precheck_exit_triggers(bad))

    def test_limit_order_mode_not_checked(self):
        """限价挂单不是条件单，不受此规则约束。"""
        ex = self._ex(TriggerClient(mark=86000.0))
        it = make_intent(tp=85500.0, sl=86500.0, tp_mode="limit_order", sl_mode="limit_order")
        self.assertIsNone(ex._precheck_exit_triggers(it))

    def test_tp2_tp3_also_checked(self):
        ex = self._ex(TriggerClient(mark=86000.0))
        err = ex._precheck_exit_triggers(make_intent(tp=87000.0, tp2=86500.0, tp3=85000.0, sl=85000.0))
        self.assertIsNotNone(err)
        self.assertIn("tp=85000", err)

    def test_no_ticker_does_not_block(self):
        """取不到参考价时不拦，保持原行为。"""
        ex = self._ex(TriggerClient(ticker_error=True))
        self.assertIsNone(ex._precheck_exit_triggers(make_intent(tp=1.0, sl=2.0)))


class TestRollbackUnprotectedEntry(unittest.TestCase):
    def _ex(self, client, alert_store=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Executor(client, label_prefix="pt", root=Path(td.name), bot_id="t",
                        alert_store=alert_store)

    def test_unfilled_entry_cancelled(self):
        """入场单没成交 → 撤掉，回到「什么都没挂」。"""
        c = TriggerClient()
        ex = self._ex(c)
        note = ex._rollback_unprotected_entry(
            make_intent(tp=87000.0, sl=85000.0),
            {"id": "12345", "size": 51, "left": 51, "status": "open"})
        self.assertIn("cancelled_entry:12345", note)
        self.assertEqual(c.cancelled, ["12345"])
        self.assertEqual(c.closed, [])

    def test_partially_filled_entry_does_not_close(self):
        """部分成交 → 只告警，不自动平仓。"""
        c = TriggerClient()
        ex = self._ex(c)
        note = ex._rollback_unprotected_entry(
            make_intent(tp=87000.0, sl=85000.0),
            {"id": "999", "size": 51, "left": 20, "status": "open"})
        self.assertIn("alert_only", note)
        self.assertEqual(c.closed, [])
        self.assertEqual(c.cancelled, [])

    def test_filled_entry_does_not_close(self):
        """全部成交 → 同样不自动平仓（自动平仓路径未经实盘验证）。"""
        c = TriggerClient()
        ex = self._ex(c)
        note = ex._rollback_unprotected_entry(
            make_intent(action="open_short", tp=85000.0, sl=87000.0,
                        trigger_rule_tp=2, trigger_rule_sl=1),
            {"id": "1", "size": 51, "left": 0, "status": "finished", "finish_as": "filled"})
        self.assertIn("alert_only", note)
        self.assertEqual(c.closed, [])

    def test_filled_entry_raises_alert(self):
        """已成交且保护单没挂上 → 必须落盘告警，供人工发现。"""
        c = TriggerClient()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        store = AlertStore(Path(td.name), "t")
        ex = Executor(c, label_prefix="pt", root=Path(td.name), bot_id="t",
                      alert_store=store)
        ex._rollback_unprotected_entry(
            make_intent(tp=87000.0, sl=85000.0),
            {"id": "7", "size": 51, "left": 0, "status": "finished", "finish_as": "filled"})
        alerts = read_alerts(Path(td.name), "t")
        self.assertTrue(any(a.get("type") == "unprotected_entry" for a in alerts),
                        f"未落盘 unprotected_entry 告警: {alerts}")

    def test_no_order_id_skips(self):
        c = TriggerClient()
        ex = self._ex(c)
        self.assertIn("skip", ex._rollback_unprotected_entry(
            make_intent(tp=87000.0, sl=85000.0), {}))


if __name__ == "__main__":
    unittest.main()
