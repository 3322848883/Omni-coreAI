# -*- coding: utf-8 -*-
"""SL 移动纪律回归测试（用户 2026-10-12 晚复盘修正版）。

规则判据 = **持仓是否浮盈**（mark vs entry），不是新 SL 相对 entry：
1. 浮盈才许动（mark 取不到时退回保守判据：多头新 SL >= entry）
2. 只许收紧方向（多头新 SL > 旧 SL；同值/放宽拒绝——同值重挂 churn 顺带被拦）
3. 即时触发护栏（多头新 SL >= mark 会挂上即触发，拒绝）

实盘 18:01 实例（本规则的立论）：浮盈 +7.7 点时 SL 2539.3→2545.9（仍 <
entry 2551.2）是合法锁盈（锁住 5.3 点），首版判据（新 SL vs entry）误拒。
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
    def __init__(self, positions=None, price_orders=None):
        self._positions = positions or []
        self._price_orders = price_orders or []

    def get_positions(self):
        return list(self._positions)

    def list_price_orders(self, symbol):
        return list(self._price_orders)


def _long(entry=2551.2, size=6, mark=None):
    return [{"contract": "ETH_USDT", "size": size, "entry_price": str(entry),
             "mark_price": str(mark if mark is not None else entry),
             "mode": "single_way"}]


def _short(entry=2600.0, size=-6, mark=None):
    return [{"contract": "ETH_USDT", "size": size, "entry_price": str(entry),
             "mark_price": str(mark if mark is not None else entry),
             "mode": "single_way"}]


def _sl_order(price, text="t-bre-xxx-sl"):
    return {"initial": {"text": text, "trigger_price": str(price)},
            "trigger": {"price": str(price)}, "status": "open"}


class TestSlMoveRejectedHelper(unittest.TestCase):
    """纯函数 helper：浮盈 gate + 收紧方向 + 即时触发护栏。"""

    # ---- 浮盈 gate ----
    def test_long_profit_lock_below_entry_allowed(self):
        """18:01 实例：浮盈 +7.7（mark 2558.9 > entry 2551.2），
        SL 2539.3→2545.9（< entry）= 合法锁盈，放行。"""
        self.assertEqual("", _sl_move_rejected(
            "long", 2551.2, 2545.9, old_sl=2539.3, mark=2558.9))

    def test_long_not_floating_rejected(self):
        """浮亏（mark < entry）→ 一律拒绝（即使新 SL > 旧 SL）。"""
        self.assertIn("not floating", _sl_move_rejected(
            "long", 2551.2, 2548.0, old_sl=2539.3, mark=2545.0))

    def test_short_profit_lock_above_entry_allowed(self):
        """空头浮盈（mark 2580 < entry 2600），SL 2612→2606（仍 > entry）放行。"""
        self.assertEqual("", _sl_move_rejected(
            "short", 2600.0, 2606.0, old_sl=2612.0, mark=2580.0))

    def test_short_not_floating_rejected(self):
        self.assertIn("not floating", _sl_move_rejected(
            "short", 2600.0, 2615.0, old_sl=2612.0, mark=2605.0))

    # ---- mark 缺失 → 保守回退 ----
    def test_mark_missing_long_conservative(self):
        self.assertIn("mark unknown", _sl_move_rejected(
            "long", 2551.2, 2545.9, old_sl=2539.3, mark=0.0))
        self.assertEqual("", _sl_move_rejected(
            "long", 2551.2, 2551.2, old_sl=2539.3, mark=0.0))

    # ---- 只许收紧方向 ----
    def test_long_widen_rejected(self):
        """放宽止损空间（新 SL < 旧 SL）→ 拒绝。"""
        self.assertIn("widen", _sl_move_rejected(
            "long", 2551.2, 2530.0, old_sl=2539.3, mark=2558.9))

    def test_long_same_value_rejected(self):
        """同值重挂 churn 在此被拦。"""
        self.assertIn("widen/no-op", _sl_move_rejected(
            "long", 2551.2, 2545.9, old_sl=2545.9, mark=2558.9))

    def test_short_widen_rejected(self):
        self.assertIn("widen", _sl_move_rejected(
            "short", 2600.0, 2615.0, old_sl=2606.0, mark=2580.0))

    # ---- 即时触发护栏 ----
    def test_long_sl_at_or_above_mark_rejected(self):
        self.assertIn("instantly", _sl_move_rejected(
            "long", 2551.2, 2559.0, old_sl=2539.3, mark=2558.9))

    def test_short_sl_at_or_below_mark_rejected(self):
        self.assertIn("instantly", _sl_move_rejected(
            "short", 2600.0, 2579.0, old_sl=2612.0, mark=2580.0))

    # ---- 保本边界 ----
    def test_long_break_even_allowed_when_floating(self):
        self.assertEqual("", _sl_move_rejected(
            "long", 2551.2, 2551.2, old_sl=2539.3, mark=2558.9))


class TestModifySlDiscipline(unittest.TestCase):
    """executor 集成：被拒 → 良性 no-op（纯 SL）/丢 SL 腿（TP+SL），旧 SL 保留。"""

    def _ex(self, positions, price_orders=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Executor(FakeClient(positions, price_orders), label_prefix="bre",
                        root=Path(td.name), bot_id="t")

    def test_not_floating_rejected_noop(self):
        """浮亏时收紧 SL → 拒、保留旧 SL（旧 SL 由 owned 推导）。"""
        res = self._ex(
            _long(entry=2551.2, mark=2545.0),
            [_sl_order(2539.3)],
        )._modify_tp_sl(
            Intent(action="modify_tp_sl", symbol="ETH_USDT",
                   side="long", sl=2548.0))
        self.assertTrue(res.ok, "纯 SL 修改被拒不应算失败（旧 SL 仍有效）")
        self.assertIn("noop", res.detail)
        self.assertIn("not floating", res.detail["sl_rejected"])

    def test_same_value_rejected_noop(self):
        """同值重挂（实盘 14:05 段 SL 2550.0 连挂 4 次）→ 现在被拦成 no-op。"""
        res = self._ex(
            _long(entry=2559.2, mark=2560.0),
            [_sl_order(2550.0)],
        )._modify_tp_sl(
            Intent(action="modify_tp_sl", symbol="ETH_USDT",
                   side="long", sl=2550.0))
        self.assertTrue(res.ok)
        self.assertIn("noop", res.detail)
        self.assertIn("widen/no-op", res.detail["sl_rejected"])

    def test_profit_lock_proceeds_past_gate(self):
        """合法锁盈（18:01 场景）→ 不被纪律拦，继续走到 placement
        （FakeClient 缺 get_contract → AttributeError = 证明放行）。"""
        with self.assertRaises(AttributeError):
            self._ex(
                _long(entry=2551.2, mark=2558.9),
                [_sl_order(2539.3)],
            )._modify_tp_sl(
                Intent(action="modify_tp_sl", symbol="ETH_USDT",
                       side="long", sl=2545.9))

    def test_tp_plus_sl_reject_drops_sl_leg(self):
        """浮亏 + TP/SL 同改 → 丢 SL 腿、TP 照改（继续执行，不做 no-op）。

        placement 需要完整 FakeClient，这里断言到能断言的深度：
        异常发生在 get_contract（FakeClient 缺方法）= 没有在纪律处提前
        no-op 返回；且 intent.sl 已被丢弃。"""
        intent = Intent(action="modify_tp_sl", symbol="ETH_USDT",
                        side="long", tp=2570.0, sl=2548.0)
        with self.assertRaises(AttributeError):
            self._ex(
                _long(entry=2551.2, mark=2545.0),
                [_sl_order(2539.3)],
            )._modify_tp_sl(intent)
        self.assertIsNone(intent.sl, "SL 腿应被纪律丢弃（TP 照改）")


class TestLadderSlPolicy(unittest.TestCase):
    """policy="ladder"（阶梯/马丁网格，2026-10-13 新增）。

    跳过浮盈 gate + 允许往深挪（widen），仅保留「同值 no-op」与「即时触发」护栏。

    立论（实盘）：martin-grid 05:44 / 07:02 / 08:06 三次「没跑满 10 层就止损」——
    `peak_trail` 把全梯共用 SL 从 2245 一路推到 2431（浅于马档之上），模型连续
    9 轮 CoT 想改回 2344/2320 全被拦：not floating ×25 + widen ×14。
    """

    # ---- 浮亏 + 收紧：ladder 放行（strict 会拒 not floating）----
    def test_ladder_not_floating_allowed(self):
        self.assertEqual("", _sl_move_rejected(
            "long", 2551.2, 2540.0, old_sl=2539.3, mark=2545.0, policy="ladder"))

    # ---- widen：把被推浅的 SL 挪回结构位（本次修复的核心场景）----
    def test_ladder_widen_long_allowed(self):
        """实盘原型：SL 2545.9（被 peak_trail 推浅）→ 2344.0（最深档之外）。"""
        self.assertEqual("", _sl_move_rejected(
            "long", 2551.2, 2344.0, old_sl=2545.9, mark=2500.0, policy="ladder"))

    def test_ladder_widen_short_allowed(self):
        self.assertEqual("", _sl_move_rejected(
            "short", 2600.0, 2650.0, old_sl=2606.0, mark=2605.0, policy="ladder"))

    # ---- 同值仍拦（churn）----
    def test_ladder_same_value_blocked(self):
        """mark 高于同值 SL（避免撞即时触发护栏）→ 命中 no-op churn。"""
        self.assertIn("no-op churn", _sl_move_rejected(
            "long", 2551.2, 2545.9, old_sl=2545.9, mark=2560.0, policy="ladder"))

    # ---- 即时触发护栏仍生效（防挂上即平仓）----
    def test_ladder_instant_trigger_long_blocked(self):
        self.assertIn("instantly", _sl_move_rejected(
            "long", 2551.2, 2500.0, old_sl=2490.0, mark=2500.0, policy="ladder"))

    def test_ladder_instant_trigger_short_blocked(self):
        self.assertIn("instantly", _sl_move_rejected(
            "short", 2600.0, 2605.0, old_sl=2612.0, mark=2605.0, policy="ladder"))

    # ---- mark 缺失：ladder 不做保守回退（浮亏常态，不能靠 entry 卡）----
    def test_ladder_mark_missing_allowed(self):
        self.assertEqual("", _sl_move_rejected(
            "long", 2551.2, 2344.0, old_sl=2545.9, mark=0.0, policy="ladder"))

    # ---- 策略别名 + 大小写/空白容错 ----
    def test_policy_aliases(self):
        for p in ("grid", "widen", "free", " LADDER "):
            self.assertEqual("", _sl_move_rejected(
                "long", 2551.2, 2344.0, old_sl=2545.9, mark=2500.0, policy=p))

    # ---- 默认仍是 strict：brooks 类行为零变化 ----
    def test_default_still_strict(self):
        self.assertIn("not floating", _sl_move_rejected(
            "long", 2551.2, 2548.0, old_sl=2539.3, mark=2545.0))
        self.assertIn("not floating", _sl_move_rejected(
            "long", 2551.2, 2548.0, old_sl=2539.3, mark=2545.0, policy="strict"))


class TestLadderSlPolicyIntegration(unittest.TestCase):
    """executor 集成：`account_risk.sl_move_policy="ladder"` 时浮亏 widen 放行。

    放行判据 = 纪律处**没有**提前 no-op 返回，继续走到 placement（FakeClient
    缺 `get_contract` → AttributeError 即为放行证据，与既有测试同法）。
    """

    def _ex(self, account_risk, positions, price_orders=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        # label_prefix 必须与 `_sl_order` 的 `t-bre-` 前缀一致，否则 own-scope
        # 过滤把这条 SL 当别人的 → old_sl=0 → 纪律分支被跳过（测试失真）。
        return Executor(FakeClient(positions, price_orders), label_prefix="bre",
                        root=Path(td.name), bot_id="martin-grid",
                        account_risk=account_risk)

    def test_ladder_policy_lets_widen_through(self):
        """浮亏 + 把 SL 从 2545.9 挪回 2344 → 不再被纪律拦住。"""
        with self.assertRaises(AttributeError):
            self._ex(
                {"sl_move_policy": "ladder"},
                _long(entry=2551.2, mark=2500.0),
                [_sl_order(2545.9)],
            )._modify_tp_sl(
                Intent(action="modify_tp_sl", symbol="ETH_USDT",
                       side="long", sl=2344.0))

    def test_strict_policy_still_blocks_same_case(self):
        """同一场景（无 policy / strict）→ 仍被拒成良性 no-op（回归保护）。

        浮亏 widen 在 strict 下先撞 `not floating`（网格场景全线锁死的原因）。"""
        res = self._ex(
            {},
            _long(entry=2551.2, mark=2500.0),
            [_sl_order(2545.9)],
        )._modify_tp_sl(
            Intent(action="modify_tp_sl", symbol="ETH_USDT",
                   side="long", sl=2344.0))
        self.assertTrue(res.ok)
        self.assertIn("noop", res.detail)
        self.assertIn("not floating", res.detail["sl_rejected"])

    def test_strict_widen_still_blocked_when_floating(self):
        """浮盈时 strict 也拦 widen（只许收紧）——与 ladder 的关键差异。"""
        res = self._ex(
            {"sl_move_policy": "strict"},
            _long(entry=2551.2, mark=2560.0),
            [_sl_order(2545.9)],
        )._modify_tp_sl(
            Intent(action="modify_tp_sl", symbol="ETH_USDT",
                   side="long", sl=2344.0))
        self.assertTrue(res.ok)
        self.assertIn("noop", res.detail)
        self.assertIn("widen/no-op", res.detail["sl_rejected"])

    def test_ladder_same_value_still_noop(self):
        """ladder 下同值重挂仍拦成 no-op（churn 保护不丢）。"""
        res = self._ex(
            {"sl_move_policy": "ladder"},
            _long(entry=2551.2, mark=2560.0),
            [_sl_order(2545.9)],
        )._modify_tp_sl(
            Intent(action="modify_tp_sl", symbol="ETH_USDT",
                   side="long", sl=2545.9))
        self.assertTrue(res.ok)
        self.assertIn("noop", res.detail)
        self.assertIn("no-op churn", res.detail["sl_rejected"])


if __name__ == "__main__":
    unittest.main()
