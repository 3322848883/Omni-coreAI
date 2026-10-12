# -*- coding: utf-8 -*-
"""SL 移动纪律回归测试（Brooks / 用户 2026-10-12）。

背景：17:15 那单 entry 2551.2，SL 从 2539.3 收到 2545.9 仍 < entry —— 既没锁
利润、又把止损空间从 11.9pt 压到 5.3pt，一波动就被打。modify_tp_sl 路径此前
只校验白名单 + tp/sl 存在性（risk.py:115-125），不校验 SL 相对 entry 的方向。

规则：SL 只能往保本/锁盈方向移。
- 多头：新 SL >= entry 放行；< entry 拒绝（保留旧 SL）
- 空头：新 SL <= entry 放行；> entry 拒绝
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor, _sl_move_rejected  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402


class FakeClient:
    def __init__(self, positions=None):
        self._positions = positions or []

    def get_positions(self):
        return list(self._positions)


def _long(entry=2551.2, size=6):
    return [{"contract": "ETH_USDT", "size": size, "entry_price": str(entry),
             "mark_price": str(entry), "mode": "single_way"}]


def _short(entry=2600.0, size=-6):
    return [{"contract": "ETH_USDT", "size": size, "entry_price": str(entry),
             "mark_price": str(entry), "mode": "single_way"}]


class TestSlMoveRejectedHelper(unittest.TestCase):
    """纯函数 helper：方向 + 边界。"""

    def test_long_loss_side_rejected(self):
        self.assertIn("loss-side", _sl_move_rejected("long", 2551.2, 2545.9))

    def test_long_break_even_allowed(self):
        self.assertEqual("", _sl_move_rejected("long", 2551.2, 2551.2))

    def test_long_profit_side_allowed(self):
        self.assertEqual("", _sl_move_rejected("long", 2551.2, 2560.0))

    def test_short_loss_side_rejected(self):
        self.assertIn("loss-side", _sl_move_rejected("short", 2600.0, 2610.0))

    def test_short_break_even_allowed(self):
        self.assertEqual("", _sl_move_rejected("short", 2600.0, 2600.0))

    def test_short_profit_side_allowed(self):
        self.assertEqual("", _sl_move_rejected("short", 2600.0, 2590.0))


class TestModifySlDiscipline(unittest.TestCase):
    """executor 集成：纯 SL 修改被拒 → 良性 no-op，旧 SL 保留。"""

    def _ex(self, positions):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Executor(FakeClient(positions), label_prefix="bre",
                        root=Path(td.name), bot_id="t")

    def test_long_sl_below_entry_rejected_noop(self):
        """复现实盘场景：entry 2551.2、新 SL 2545.9（浮亏侧）→ 拒、保留旧 SL。"""
        res = self._ex(_long(entry=2551.2))._modify_tp_sl(
            Intent(action="modify_tp_sl", symbol="ETH_USDT",
                   side="long", sl=2545.9))
        self.assertTrue(res.ok, "纯 SL 修改被拒不应算失败（旧 SL 仍有效）")
        self.assertIn("noop", res.detail)
        self.assertIn("sl_rejected", res.detail)
        self.assertIn("2545.9", res.detail["sl_rejected"])

    def test_short_sl_above_entry_rejected_noop(self):
        res = self._ex(_short(entry=2600.0))._modify_tp_sl(
            Intent(action="modify_tp_sl", symbol="ETH_USDT",
                   side="short", sl=2610.0))
        self.assertTrue(res.ok)
        self.assertIn("noop", res.detail)
        self.assertIn("2610.0", res.detail["sl_rejected"])

    def test_long_sl_at_break_even_proceeds_past_gate(self):
        """新 SL == entry（保本）→ 不被纪律早拦，继续往下走（到 get_contract
        因 FakeClient 缺方法而 AttributeError = 证明没走 noop 拒绝路径）。"""
        with self.assertRaises(AttributeError):
            self._ex(_long(entry=2551.2))._modify_tp_sl(
                Intent(action="modify_tp_sl", symbol="ETH_USDT",
                       side="long", sl=2551.2))


if __name__ == "__main__":
    unittest.main()
