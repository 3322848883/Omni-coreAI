# -*- coding: utf-8 -*-
"""同向同价 SL 归并（2026-10-13，P1）回归测试。

**背景**：martin-grid 阶梯是**逐档**调 `_open` 建仓的，每档都带自己的 `intent.sl`。
照着「每档挂一条 size=本档张数」写，盘上就会累积出 N 条同价小单 —— 实测 4 条
`t-mtg-sl`，sz 9/1/1/6。而人格的设计是**全梯共用一条**（`prompts/ladder_nogate.md`
§六），`_modify_tp_sl` 路径挂的也本来就是「一条 size=持仓总量」：两条路径口径不一致
正是分腿来源。分腿一旦生成、再被 `peak_trail` 各自推浅，就演化成「同一阶梯上前浅
后深的多条 SL」→ 价格回踩时先打浅的那条 → **没跑满 10 层就止损**。

**修复**：
1. `_plan_shared_sl()`：`_open` 挂 SL 时把**同向同价**旧单并进来，新单张数 =
   旧单之和 + 本档（**钳制在真实敞口内**：持仓 + 已挂未成交同向入场单）；旧单由
   调用方在新单**落盘后**撤（先挂后撤，不留无保护空窗）。
2. `_resync_protectors()`：SL 按 `(方向, 触发价)` 归并，同一价位只留一条、
   张数 = **该方向**持仓量（原先逐条重挂 → 4 条各放大到 total → 超配数倍）。

**不归并的场景**：人格 §4.3 的「远端档自带止损」落在**不同价位**，属于独立仓位，
两类靠价格比较分开。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta  # noqa: E402
from omnialpha.schema import Intent, infer_trigger_rules  # noqa: E402


META = ContractMeta(name="ETH_USDT", quanto_multiplier=0.01,
                    order_size_round=1, order_price_round=0.05, leverage_max=50)


class FakeClient:
    def __init__(self, positions=None, price_orders=None, open_orders=None):
        self._positions = list(positions or [])
        self._price_orders = list(price_orders or [])
        self._open_orders = list(open_orders or [])
        self.placed = []       # place_price_order 收到的 body
        self.cancelled = []    # cancel_price_order 收到的 id

    def get_positions(self):
        return list(self._positions)

    def list_price_orders(self, symbol):
        return list(self._price_orders)

    def list_orders(self, symbol):
        return list(self._open_orders)

    def get_contract(self, symbol):
        return META

    def get_last_price(self, symbol):
        return 2300.0

    def place_price_order(self, body):
        self.placed.append(body)
        return {"id": "new%d" % len(self.placed)}

    def cancel_price_order(self, pid):
        self.cancelled.append(str(pid))
        return {"id": pid}


def _pos(size, entry=2500.0):
    return {"contract": "ETH_USDT", "size": size, "entry_price": str(entry),
            "mark_price": str(entry), "mode": "single_way"}


def _buy(pid, size, left=None, text="t-mtg"):
    """未成交的买单（建多头）—— 用于 `_sl_cover_cap` 的敞口上限。"""
    return {"id": str(pid), "text": text, "size": size,
            "left": size if left is None else left, "status": "open"}


def _sl(pid, price, size, text="t-mtg-sl", status="open"):
    """触发型 SL：多头保护 size 为负、空头为正（符号即方向）。"""
    return {"id": str(pid),
            "initial": {"text": text, "trigger_price": str(price),
                        "size": size, "reduce_only": True},
            "trigger": {"price": str(price), "rule": 2, "price_type": 0},
            "status": status}


def _tp(pid, price, size, text="t-mtg-tp"):
    return {"id": str(pid),
            "initial": {"text": text, "trigger_price": str(price),
                        "size": size, "reduce_only": True},
            "trigger": {"price": str(price), "rule": 1, "price_type": 0},
            "status": "open"}


class TestPlanSharedSl(unittest.TestCase):
    """`_plan_shared_sl`：算张数 + 挑出该撤的同价旧单（不执行任何动作）。"""

    def _ex(self, price_orders=None, open_orders=None, positions=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        if positions is None:
            positions = [_pos(6)]
        cli = FakeClient(positions, price_orders, open_orders)
        return Executor(cli, label_prefix="mtg", root=Path(td.name),
                        bot_id="martin-grid"), cli

    def test_no_existing_sl(self):
        """首条 SL：张数 = 本档，无旧单可撤。"""
        ex, _ = self._ex([], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)
        self.assertEqual([], ids)

    def test_single_same_price_merged(self):
        """持仓 6 + 旧 SL 6 张 + 本档买单 1 张 → 7（敞口上限 6+1=7）。"""
        ex, _ = self._ex([_sl("a", 2325.0, -6)], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(7, size)
        self.assertEqual(["a"], ids)

    def test_multi_same_price_merged(self):
        """实盘原型：持仓 17 + 4 条同价 2325（sz 9/1/1/6）→ 本档 +1 → 18。"""
        ex, _ = self._ex(
            [_sl("a", 2325.0, -9), _sl("b", 2325.0, -1),
             _sl("c", 2325.0, -1), _sl("d", 2325.0, -6)],
            [_buy("e", 1)],
            positions=[_pos(17)],
        )
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(18, size)
        self.assertEqual(["a", "b", "c", "d"], ids)

    def test_different_price_not_merged(self):
        """人格 §4.3：远端档自带止损（不同价）→ 不归并、不撤。"""
        ex, _ = self._ex([_sl("a", 2280.0, -6)], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)
        self.assertEqual([], ids)

    def test_mixed_only_same_price_merged(self):
        ex, _ = self._ex([_sl("a", 2325.0, -6), _sl("b", 2280.0, -9)], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(7, size)
        self.assertEqual(["a"], ids)

    def test_opposite_side_not_merged(self):
        """空头 SL（size>0）不该被多头归并进来 —— 双向持仓各自一条。"""
        ex, _ = self._ex([_sl("a", 2325.0, 6)], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)
        self.assertEqual([], ids)

    def test_tick_tolerance(self):
        """半个 tick 内的价差视作同价（round_price 后相等）。"""
        ex, _ = self._ex([_sl("a", 2325.02, -6)], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(7, size)
        self.assertEqual(["a"], ids)

    def test_dead_order_ignored(self):
        """已终结的 SL 不参与归并。"""
        ex, _ = self._ex([_sl("a", 2325.0, -6, status="cancelled")], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)
        self.assertEqual([], ids)

    def test_other_bot_not_merged(self):
        """别的 bot 命名空间（t-bre-*）不碰。"""
        ex, _ = self._ex([_sl("a", 2325.0, -6, text="t-bre-sl")], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)
        self.assertEqual([], ids)

    def test_tp_not_counted(self):
        """只归并 SL；TP 腿（-tp）不算入张数。"""
        ex, _ = self._ex([_tp("a", 2325.0, -6)], [_buy("e", 1)])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)
        self.assertEqual([], ids)

    def test_cap_clamps_to_real_exposure(self):
        """旧 SL 被外部放大（持仓 0、无挂单）→ 钳到 0 → 不归并（不挂超配单）。"""
        ex, _ = self._ex([_sl("a", 2325.0, -17)], [], positions=[])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)
        self.assertEqual([], ids)

    def test_cap_uses_pending_entry_orders(self):
        """挂单阶段（持仓 0、本档 1 张买单）→ 上限 1，**钳住**旧 SL 的 3 张。"""
        ex, _ = self._ex([_sl("a", 2325.0, -3)], [_buy("e1", 1)], positions=[])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)      # min(3+1, 0+1)
        self.assertEqual(["a"], ids)

    def test_cap_accumulates_across_ladder_levels(self):
        """逐档建仓（持仓 0）：每档累加，最终一条 = 全部已挂买单张数。"""
        ex, _ = self._ex([_sl("a", 2325.0, -1), _sl("b", 2325.0, -2)],
                         [_buy("e1", 1), _buy("e2", 2), _buy("e3", 1)],
                         positions=[])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(4, size)      # min(1+2+1, 0+4)
        self.assertEqual(["a", "b"], ids)

    def test_other_bot_pending_not_counted(self):
        """别的 bot 的挂单不算进敞口。"""
        ex, _ = self._ex([_sl("a", 2325.0, -6)],
                         [_buy("e", 50, text="t-bre")], positions=[])
        size, ids = ex._plan_shared_sl("ETH_USDT", "long", 2325.0, 1, META)
        self.assertEqual(1, size)
        self.assertEqual([], ids)


class TestResyncProtectorsMerge(unittest.TestCase):
    """`_resync_protectors`：SL 归并 + TP 保持原逻辑。"""

    def _ex(self, positions, price_orders=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        if isinstance(positions, int):      # 便利：单腿持仓直接给张数
            positions = [_pos(positions)]
        cli = FakeClient(positions, price_orders)
        return Executor(cli, label_prefix="mtg", root=Path(td.name),
                        bot_id="martin-grid"), cli

    def test_four_same_price_merged_into_one(self):
        """实盘原型：4 条同价 2325（9/1/1/6）、持仓 17 → 归并成 1 条 17。"""
        ex, cli = self._ex(17, [_sl("a", 2325.0, -9), _sl("b", 2325.0, -1),
                               _sl("c", 2325.0, -1), _sl("d", 2325.0, -6)])
        notes = ex._resync_protectors("ETH_USDT")
        merged = [n for n in notes if "merged_sl" in n]
        self.assertEqual(1, len(merged))
        self.assertEqual(17, merged[0]["to_size"])
        self.assertEqual(4, merged[0]["n"])
        self.assertEqual(1, len(cli.placed))
        self.assertEqual(-17, cli.placed[0]["initial"]["size"])
        self.assertEqual(sorted(["a", "b", "c", "d"]), sorted(cli.cancelled))

    def test_single_correct_left_alone(self):
        """单条且张数正确 → 不动（避免无谓 churn）。"""
        ex, cli = self._ex(17, [_sl("a", 2325.0, -17)])
        notes = ex._resync_protectors("ETH_USDT")
        self.assertEqual([], [n for n in notes if "merged_sl" in n])
        self.assertEqual([], cli.placed)
        self.assertEqual([], cli.cancelled)

    def test_single_wrong_size_resized(self):
        """单条但张数不足（9 vs 17）→ 撤 1 挂 1、size=17。"""
        ex, cli = self._ex(17, [_sl("a", 2325.0, -9)])
        ex._resync_protectors("ETH_USDT")
        self.assertEqual(-17, cli.placed[0]["initial"]["size"])
        self.assertEqual(["a"], cli.cancelled)

    def test_dual_side_uses_per_side_size(self):
        """双向持仓：long SL 按 long 持仓量归并（不拿全 symbol 的 total）。"""
        ex, cli = self._ex([_pos(10), _pos(-4)], [
            _sl("a", 2325.0, -6), _sl("b", 2325.0, -4),   # long 两条同价
            _sl("c", 2600.0, 4),                           # short 一条且正确
        ])
        notes = ex._resync_protectors("ETH_USDT")
        merged = {n["side"]: n for n in notes if "merged_sl" in n}
        self.assertEqual(10, merged["long"]["to_size"])
        self.assertNotIn("short", merged)
        self.assertEqual(1, len(cli.placed))
        self.assertEqual(-10, cli.placed[0]["initial"]["size"])
        self.assertEqual(sorted(["a", "b"]), sorted(cli.cancelled))

    def test_tp_legs_not_merged(self):
        """TP 腿保持原逻辑：每条各自重挂（size=total），不做归并。"""
        ex, cli = self._ex(17, [_tp("a", 2600.0, -6), _tp("b", 2650.0, -4)])
        notes = ex._resync_protectors("ETH_USDT")
        self.assertEqual([], [n for n in notes if "merged_sl" in n])
        self.assertEqual(2, len(cli.placed))
        self.assertEqual(sorted(["a", "b"]), sorted(cli.cancelled))

    def test_side_without_position_untouched(self):
        """某方向已无持仓 → 其遗留 SL 交给 orphan 清理，本方法不动。"""
        ex, cli = self._ex([_pos(-5)], [_sl("a", 2325.0, -9)])
        ex._resync_protectors("ETH_USDT")
        self.assertEqual([], cli.placed)
        self.assertEqual([], cli.cancelled)

    def test_different_price_groups_each_merged(self):
        """不同价位的两组各自归并成一条（§4.3 的远端档结构保持独立）。"""
        ex, cli = self._ex(17, [_sl("a", 2325.0, -6), _sl("b", 2325.0, -6),
                               _sl("c", 2280.0, -5)])
        notes = ex._resync_protectors("ETH_USDT")
        merged = [n for n in notes if "merged_sl" in n]
        # 2325 组：两条 → 合成 17；2280 组：单条但张数不足 5≠17 → 也撤重挂
        self.assertEqual(2, len(merged))
        self.assertEqual({2325.0, 2280.0},
                         {float(n["trigger"]) for n in merged})


class TestOpenMergesSharedSl(unittest.TestCase):
    """`_open` 集成：同价旧 SL 并进来、新单用合并张数、旧单在**新单落盘后**撤。

    重依赖（风控闸门 / 前置校验 / 入场腿）用 mock 短路，只保留本改动的契约：
    SL 段的张数来源 + 撤旧时机。
    """

    def _run_open(self, positions, price_orders, intent, open_orders=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        cli = FakeClient(positions, price_orders, open_orders)
        ex = Executor(cli, label_prefix="mtg", root=Path(td.name),
                      bot_id="martin-grid")
        calls = []

        def fake_exit(self, placer, kind, price_order):
            rec = placer()                      # 真跑放置器 → 记进 cli.placed
            calls.append((kind, cli.placed[-1]))
            return {"order": {"id": "new-%s" % kind}}, None

        patches = [
            mock.patch.object(Executor, "_check_symbol", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_safe_mode", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_no_flip", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_open_sl", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_account_risk", lambda *a, **k: None),
            mock.patch.object(Executor, "_check_notional", lambda *a, **k: None),
            mock.patch.object(Executor, "_precheck_exit_triggers", lambda *a, **k: ""),
            mock.patch.object(Executor, "_place_entry_leg",
                              lambda *a, **k: ({"order": {"id": "e1"}}, None)),
            mock.patch.object(Executor, "_place_exit_leg", fake_exit),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        res = ex._open(intent)
        return res, cli, calls

    @staticmethod
    def _intent(size=1):
        it = Intent(action="open_long", symbol="ETH_USDT", size=size,
                    sl=2325.0, label="mtg")
        it.trigger_rule_sl = infer_trigger_rules("open_long", False)
        return it

    def test_open_merges_same_price_and_cancels_after_place(self):
        """持仓 6 + 两条同价旧 SL（6+1）+ 本档买单 1 → 归并成 1 条 size=7。"""
        res, cli, calls = self._run_open(
            [_pos(6)],
            [_sl("old1", 2325.0, -6), _sl("old2", 2325.0, -1)],
            self._intent(size=1),
            open_orders=[_buy("e1", 1)],
        )
        self.assertTrue(res.ok, res.error)
        sl_placed = [b for _k, b in calls if b["initial"]["text"] == "t-mtg-sl"]
        self.assertEqual(1, len(sl_placed))
        self.assertEqual(-7, sl_placed[0]["initial"]["size"])   # min(6+1+1, 6+1)
        self.assertEqual(sorted(["old1", "old2"]), sorted(cli.cancelled))

    def test_open_no_existing_uses_own_size(self):
        res, cli, calls = self._run_open([_pos(6)], [], self._intent(size=6),
                                         open_orders=[_buy("e1", 6)])
        self.assertTrue(res.ok, res.error)
        sl_placed = [b for _k, b in calls if b["initial"]["text"] == "t-mtg-sl"]
        self.assertEqual(-6, sl_placed[0]["initial"]["size"])
        self.assertEqual([], cli.cancelled)

    def test_open_keeps_different_price_sl(self):
        """不同价的旧 SL 保留（远端档自带止损），只挂本档。"""
        res, cli, calls = self._run_open(
            [_pos(6)], [_sl("old1", 2280.0, -6)], self._intent(size=1),
            open_orders=[_buy("e1", 1)])
        self.assertTrue(res.ok, res.error)
        sl_placed = [b for _k, b in calls if b["initial"]["text"] == "t-mtg-sl"]
        self.assertEqual(-1, sl_placed[0]["initial"]["size"])
        self.assertEqual([], cli.cancelled)


if __name__ == "__main__":
    unittest.main()
