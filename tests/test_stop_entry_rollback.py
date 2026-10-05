"""`stop_entry_*` 的保护单挂载失败必须回滚（与 `open_*` 同源）。

回归背景（2026-10-05 架构盘点）：`_stop_entry` 的保护单挂载失败分支只返回
`exit_not_placed` 错误 —— **既不撤掉那张条件单，也不落盘告警**。
`open_*` 路径早在 `_open` 里接了 `_rollback_unprotected_entry`，突破单这条没有。

后果：若挂保护单失败期间条件单**已被触发成交**，账户留下一个**无人知晓的裸仓**
（无 SL/TP），只能等下一轮 plan 或人工发现。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402


class _Client:
    """只记录撤单调用。"""

    def __init__(self):
        self.cancelled_price = []
        self.cancelled_plain = []

    def banner(self):
        return ""

    def cancel_price_order(self, oid):
        self.cancelled_price.append(str(oid))

    def cancel_order(self, oid):
        self.cancelled_plain.append(str(oid))


def _intent(action="stop_entry_long"):
    return parse_signal({
        "action": action, "symbol": "BTC_USDT", "size_usd": 100,
        "trigger_price": 51000, "sl": 49000,
    }).intents[0]


class TestStopEntryRollback(unittest.TestCase):
    def _ex(self, client, root):
        return Executor(client, symbols_whitelist=["BTC_USDT"],
                        root=root, bot_id="b1", require_sl=False)

    def test_untriggered_stop_entry_is_cancelled(self):
        """条件单未触发 → 用 cancel_price_order 撤掉（不是 cancel_order）。"""
        client = _Client()
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(client, Path(td))
            note = ex._rollback_unprotected_entry(
                _intent(), {"id": "po-1", "status": "open", "size": 10, "left": 10},
                is_price_order=True)
        self.assertIn("cancelled_entry", note)
        self.assertEqual(client.cancelled_price, ["po-1"])
        self.assertEqual(client.cancelled_plain, [], "条件单不能用 cancel_order 撤")

    def test_plain_entry_still_uses_cancel_order(self):
        """普通入场单仍走 cancel_order（不要被改动波及）。"""
        client = _Client()
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(client, Path(td))
            note = ex._rollback_unprotected_entry(
                _intent("open_long"), {"id": "o-1", "size": 10, "left": 10})
        self.assertIn("cancelled_entry", note)
        self.assertEqual(client.cancelled_plain, ["o-1"])
        self.assertEqual(client.cancelled_price, [])

    def test_triggered_stop_entry_alerts_not_cancels(self):
        """条件单已触发成交 → 只告警，不撤单、不自动平仓。"""
        client = _Client()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(client, root)
            from omnialpha.monitoring.alerts import AlertStore, read_alerts
            ex.alert_store = AlertStore(root, "b1")
            note = ex._rollback_unprotected_entry(
                _intent(), {"id": "po-2", "size": 10, "left": 0},  # left=0 → 已成交
                is_price_order=True)
            self.assertIn("alert_only", note)
            self.assertEqual(client.cancelled_price, [], "已成交不该撤单")
            types = [a.get("type") for a in read_alerts(root, "b1")]
            self.assertIn("unprotected_entry", types)

    def test_missing_id_is_skipped(self):
        client = _Client()
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(client, Path(td))
            note = ex._rollback_unprotected_entry(
                _intent(), {"size": 10, "left": 10}, is_price_order=True)
        self.assertIn("skip", note)


if __name__ == "__main__":
    unittest.main()
