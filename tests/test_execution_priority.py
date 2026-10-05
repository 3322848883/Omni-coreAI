"""执行层：先平后开排序 + 优雅停机检查点。

参照 nofx 的 `sortDecisionsByPriority`（close=1 / open=2 / hold=3）与
`isRunning` 检查点（每轮开始 + **每条决策执行前**都查）。

我们原先：多腿信号按数组原序执行（换仓时可能因保证金不足被拒），
且只有进程级锁 —— 长批次执行中无法立即停手。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor, _sort_intents_by_priority  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402


class _I:
    def __init__(self, action):
        self.action = action


class TestPrioritySort(unittest.TestCase):
    def test_close_before_open(self):
        out = _sort_intents_by_priority(
            [_I("open_long"), _I("close_short"), _I("stop_entry_long"), _I("close_long")])
        self.assertEqual([i.action for i in out],
                         ["close_short", "close_long", "open_long", "stop_entry_long"])

    def test_full_order(self):
        out = _sort_intents_by_priority([
            _I("open_long"), _I("modify_tp_sl"), _I("cancel_price_all"),
            _I("reduce_long"), _I("close_all"), _I("hold")])
        self.assertEqual([i.action for i in out],
                         ["close_all", "reduce_long", "cancel_price_all",
                          "modify_tp_sl", "open_long", "hold"])

    def test_stable_within_same_priority(self):
        """同优先级保持原顺序（不打乱同一动作的多腿）。"""
        out = _sort_intents_by_priority(
            [_I("open_long"), _I("open_short"), _I("open_long")])
        self.assertEqual([i.action for i in out],
                         ["open_long", "open_short", "open_long"])

    def test_unknown_action_goes_late(self):
        out = _sort_intents_by_priority([_I("weird_action"), _I("close_long")])
        self.assertEqual([i.action for i in out], ["close_long", "weird_action"])

    def test_empty(self):
        self.assertEqual(_sort_intents_by_priority([]), [])


class TestStopCheckpoint(unittest.TestCase):
    def _ex(self, root):
        class _C:
            def banner(self):
                return ""
        return Executor(_C(), symbols_whitelist=["BTC_USDT"], root=root, bot_id="b1")

    def test_stop_file_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root)
            self.assertFalse(ex._stop_requested())
            p = root / "data" / "bots" / "b1" / "state" / "stop"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
            self.assertTrue(ex._stop_requested())

    def test_no_bot_id_is_false(self):
        with tempfile.TemporaryDirectory() as td:
            class _C:
                def banner(self):
                    return ""
            ex = Executor(_C(), symbols_whitelist=["BTC_USDT"], root=Path(td))
            self.assertFalse(ex._stop_requested())

    def test_stop_aborts_remaining_intents(self):
        """停机请求后，剩余 intent 不再执行（多腿信号只跑前几条）。"""
        class _C:
            def banner(self):
                return ""

            def get_positions(self):
                return []

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / "data" / "bots" / "b1" / "state" / "stop"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
            ex = Executor(_C(), symbols_whitelist=["BTC_USDT"], root=root, bot_id="b1")
            sig = parse_signal({"orders": [
                {"action": "close", "symbol": "BTC_USDT", "side": "long"},
                {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 10, "sl": 1},
            ]})
            rep = ex.execute_signal(sig)
            errs = [r.error or "" for r in rep.results]
            self.assertTrue(any("STOP_REQUESTED" in e for e in errs),
                            f"应出现停机中止：{errs}")


if __name__ == "__main__":
    unittest.main()
