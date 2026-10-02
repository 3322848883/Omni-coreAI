# -*- coding: utf-8 -*-
"""第 1 条（计划矛盾）的回归测试：executor 容错 + snapshot 修 AI 视图。

**两种成因**（实测）：
- 模拟盘：计划同轮里先 `reduce/close` 再 `modify_tp_sl` → 第二步必然 `NO_POSITION`
  （历史 103 笔，占修复后残留失败的 43%）
- 实盘：执行器挂入场单后 1–3 秒就把 TP/SL 一起挂上（不等成交），所以
  「无持仓 + 有待成交入场单 + 有保护单」是常态；AI 看到 `protections` 有单、
  `positions` 为空，误读成「有持仓可管」→ 发 `modify_tp_sl` → `NO_POSITION`
  （实盘 2026-10-02 07:59 那轮：07:44 挂的 `t-brk` 19 张未成交）

对应两处修复：
1. `Executor._modify_tp_sl()`：无持仓时返回**良性 no-op**（ok=True），不再让整轮失败
2. `snapshot.position_state()`：在账户快照里显式标注持仓状态，让 AI 不必去猜
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402
from omnialpha.strategist.snapshot import position_state  # noqa: E402


class FakeClient:
    def __init__(self, positions=None):
        self._positions = positions or []

    def get_positions(self):
        return list(self._positions)


class TestModifyNoop(unittest.TestCase):
    def _ex(self, positions=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Executor(FakeClient(positions), label_prefix="pt", root=Path(td.name), bot_id="t")

    def test_no_position_is_benign_noop(self):
        """无持仓时的 modify_tp_sl → 良性 no-op，不再算整轮失败。"""
        res = self._ex()._modify_tp_sl(
            Intent(action="modify_tp_sl", symbol="BTC_USDT", tp=87000.0, sl=85000.0))
        self.assertTrue(res.ok, "无持仓不应算失败（否则白烧周期 + 污染失败归档）")
        self.assertEqual(res.error, "")
        self.assertIn("noop", res.detail)

    def test_requires_tp_or_sl_still_fails(self):
        """两个目标价都没有 → 仍是真错误（这是调用方的问题，不该被吞）。"""
        res = self._ex()._modify_tp_sl(Intent(action="modify_tp_sl", symbol="BTC_USDT"))
        self.assertFalse(res.ok)
        self.assertIn("requires tp and/or sl", res.error)


class TestPositionState(unittest.TestCase):
    def test_position_open(self):
        s, note = position_state({"positions": [{"contract": "BTC_USDT", "size": 19}]})
        self.assertEqual(s, "position_open")
        self.assertIn("modify_tp_sl", note)

    def test_entry_pending(self):
        """核心回归：无持仓 + 有待成交入场单 → 必须明确告诉 AI「不要发 modify_tp_sl」。"""
        s, note = position_state({
            "positions": [],
            "open_orders": [{"contract": "BTC_USDT", "status": "open", "left": 19}],
            "protections": [{"contract": "BTC_USDT", "text": "t-brk-sl"}],
        })
        self.assertEqual(s, "entry_pending")
        self.assertIn("无持仓", note)
        self.assertIn("modify_tp_sl", note)

    def test_flat(self):
        s, _ = position_state({"positions": [], "open_orders": []})
        self.assertEqual(s, "flat")

    def test_filled_order_is_not_pending(self):
        """已成/已撤的挂单不算「待成交」，否则会把 flat 误判成 entry_pending。"""
        for st in ("finished", "cancelled", "closed", "failed"):
            s, _ = position_state({"positions": [], "open_orders": [{"status": st}]})
            self.assertEqual(s, "flat", f"status={st} 不应算待成交")

    def test_partially_filled_counts_as_pending(self):
        s, _ = position_state({"positions": [], "open_orders": [{"status": "partially_filled"}]})
        self.assertEqual(s, "entry_pending")

    def test_empty_account_is_flat(self):
        self.assertEqual(position_state({})[0], "flat")
        self.assertEqual(position_state(None)[0], "flat")


if __name__ == "__main__":
    unittest.main()
