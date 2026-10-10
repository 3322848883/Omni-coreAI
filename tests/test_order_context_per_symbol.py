# -*- coding: utf-8 -*-
"""T14：订单上下文与共享订单库**按币**（多币多单时每张单的上下文都进 prompt）。

覆盖 spec `symbol-as-parameter.md` §S2.4⑩：

- `_order_context_for` 原先只取 `updated_at` **最新的一张** open 单 —— 多币下一轮可能
  同时有 2–3 张单，其余币的「理由 / 前提失效价 / 最近事件」在 prompt 里凭空消失
  （D-19：模型看不到自己上一轮给另一个币定的失效价，于是反复改口）。
- 共享订单库的 `list_open` 加 `symbol` 过滤，`_resolve_order_id` 改用它 ——
  原先在这里精确比较，形态不一致就会**静默退化成不过滤**，又回到 `opens[0]` 关错币。
- 单币（或订单记录里没有 symbol）时返回**单张**，渲染形态与改动前逐字一致（I11）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.memory.context import _format_order_context, build_context  # noqa: E402
from omnialpha.persona.orders import SharedOrderStore  # noqa: E402
from omnialpha.persona.runner import PersonaRunner  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402


def _loop_runner(symbols) -> PlanRunner:
    r = PlanRunner.__new__(PlanRunner)
    r.cfg = StrategistConfig(symbols=list(symbols), bot_id="b1")
    return r


def _add_order(st, sym: str, px: float, *, account: str = "b1") -> str:
    o = st.create({"symbol": sym, "target_account": account, "side": "long",
                   "order_id": f"o-{sym}"})
    st.set_reason(o["order_id"], f"{sym} 的理由")
    st.set_premise_invalidation(o["order_id"], float(px), note="结构位")
    return o["order_id"]


class TestOrderContextPerSymbol(unittest.TestCase):
    def test_multi_symbol_keeps_every_order(self):
        """两币各一张单 → 两张的上下文（含前提失效价）都要在 prompt 里。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = SharedOrderStore(root)
            _add_order(st, "BTC_USDT", 84800)
            _add_order(st, "ETH_USDT", 2690)

            ctx = _loop_runner(["BTC_USDT", "ETH_USDT"])._order_context_for(
                root, "b1", via_group=False)
            self.assertIn("orders", ctx, "多币要返回多张的容器")
            self.assertEqual(sorted(c["symbol"] for c in ctx["orders"]),
                             ["BTC_USDT", "ETH_USDT"])

            text = _format_order_context(ctx)
            self.assertIn("84800", text, "BTC 的前提失效价丢了")
            self.assertIn("2690", text, "ETH 的前提失效价丢了")
            self.assertIn("BTC_USDT 的理由", text)
            self.assertIn("ETH_USDT 的理由", text)

    def test_single_symbol_returns_single_context(self):
        """单币 → 仍是**单张**（形态与改动前一致），渲染不含容器痕迹。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = SharedOrderStore(root)
            _add_order(st, "BTC_USDT", 84800)

            ctx = _loop_runner(["BTC_USDT"])._order_context_for(
                root, "b1", via_group=False)
            self.assertNotIn("orders", ctx)
            self.assertEqual(ctx["symbol"], "BTC_USDT")
            text = _format_order_context(ctx)
            self.assertIn("订单: o-BTC_USDT", text)
            self.assertIn("84800", text)

    def test_other_account_is_not_included(self):
        """别的账户的单不能塞进来（`via_group=False` 只看自己的账户）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = SharedOrderStore(root)
            _add_order(st, "BTC_USDT", 84800, account="other")
            self.assertIsNone(_loop_runner(["BTC_USDT"])._order_context_for(
                root, "b1", via_group=False))

    def test_no_order_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_loop_runner(["BTC_USDT"])._order_context_for(
                Path(td), "b1", via_group=False))

    def test_empty_context_renders_placeholder(self):
        self.assertEqual(_format_order_context(None), "（当前无持仓）")


class TestListOpenSymbolFilter(unittest.TestCase):
    def test_filters_and_normalises(self):
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            _add_order(st, "BTC_USDT", 84800)
            _add_order(st, "ETH_USDT", 2690)
            self.assertEqual(len(st.list_open()), 2)
            self.assertEqual([r["symbol"] for r in st.list_open(symbol="ETH_USDT")],
                             ["ETH_USDT"])
            # 小写也要命中（归一化）—— 精确比较会让它静默退化成「不过滤」
            self.assertEqual([r["symbol"] for r in st.list_open(symbol="eth_usdt")],
                             ["ETH_USDT"])


class TestResolveOrderIdPerSymbol(unittest.TestCase):
    def _runner(self, st: SharedOrderStore) -> PersonaRunner:
        r = PersonaRunner.__new__(PersonaRunner)
        r.orders = st
        r.group = SimpleNamespace(name="g1")
        return r

    def test_hold_reuses_that_symbols_order(self):
        """多币多单时 hold 必须命中**对应币**那张，不能张冠李戴。"""
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            o_btc = _add_order(st, "BTC_USDT", 84800)
            o_eth = _add_order(st, "ETH_USDT", 2690)
            st.update(o_btc, group="g1")
            st.update(o_eth, group="g1")
            r = self._runner(st)
            self.assertEqual(r._resolve_order_id("hold", {}, symbol="BTC_USDT"), o_btc)
            self.assertEqual(r._resolve_order_id("hold", {}, symbol="ETH_USDT"), o_eth)

    def test_reversal_closes_that_symbols_order_only(self):
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            o_btc = _add_order(st, "BTC_USDT", 84800)      # side=long
            o_eth = _add_order(st, "ETH_USDT", 2690)       # side=long
            st.update(o_btc, group="g1")
            st.update(o_eth, group="g1")
            r = self._runner(st)
            new_id = r._resolve_order_id("short", {}, symbol="ETH_USDT")
            self.assertNotEqual(new_id, o_eth, "方向反转要另起一张单")
            self.assertEqual(st.get(o_eth)["status"], "closed")
            self.assertEqual(st.get(o_btc)["status"], "open",
                             "BTC 那张不能被动（原先 opens[0] 会关错）")

    def test_different_symbol_does_not_reuse(self):
        """只有 BTC 有单时，对 ETH 的 hold 不该复用 BTC 的单。"""
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            o_btc = _add_order(st, "BTC_USDT", 84800)
            st.update(o_btc, group="g1")
            r = self._runner(st)
            self.assertIsNone(r._resolve_order_id("hold", {}, symbol="ETH_USDT"))


class TestOrderContextBudget(unittest.TestCase):
    """多币 prompt 的体积**有上限**（I10 成本有界）：币再多也不会线性堆进 prompt。"""

    def test_order_context_capped_at_three(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st = SharedOrderStore(root)
            for i in range(6):                      # 6 个币各有持仓
                _add_order(st, f"C{i}_USDT", 100.0 + i)
            ctx = _loop_runner([f"C{i}_USDT" for i in range(6)])._order_context_for(
                root, "b1", via_group=False)
            self.assertEqual(len(ctx["orders"]), 3, "订单上下文按币取，但上限 3 张")

    def test_recent_block_bounded_by_n_recent(self):
        """近况条数由 `n_recent` 定，**不随币数增长**（只有行内信息变长）。"""
        from omnialpha.memory.journal import MemoryJournal, tier1_journal_fields

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for c in range(6):
                chips = [{"symbol": f"C{i}_USDT", "action": "hold"} for i in range(6)]
                MemoryJournal(root, "b1").append(
                    cycle_id=f"c{c}", decision=",".join(x["action"] for x in chips),
                    reasoning="r", **tier1_journal_fields(chips))
            ctx = build_context(root, "b1", system_prompt="SYS", n_recent=3, n_index=5)
            # `[近况]` 段里只有 3 条（每条约一行）
            body = ctx["user"].split("[近况]")[1].split("[本轮快照]")[0]
            lines = [ln for ln in body.splitlines() if ln.startswith("c")]
            self.assertLessEqual(len(lines), 3, body)
            # 索引段同样只有 5 条
            idx = ctx["user"].split("[近期决策索引")[1].split("\n\n")[0]
            self.assertLessEqual(len([ln for ln in idx.splitlines() if ln.strip().startswith("c")]),
                                 5)


class TestCreateValidatesUniverse(unittest.TestCase):
    """T347：`create(universe=…)` 越界**不落盘** —— 订单库是审计面。

    写进一个本组根本不做的标的，会让「按币取单」（`_order_context_for`）与事后核对
    一起失准：那条记录会一直躺在库里，而没有任何一轮真的交易过它。
    """

    UNI = ["BTC_USDT", "ETH_USDT"]

    def test_out_of_universe_rejected_and_not_written(self):
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            with self.assertRaises(ValueError):
                st.create({"symbol": "SOL_USDT", "order_id": "o-bad"}, universe=self.UNI)
            self.assertIsNone(st.get("o-bad"), "越界的记录不得落盘")

    def test_in_universe_written(self):
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            st.create({"symbol": "ETH_USDT", "order_id": "o-ok"}, universe=self.UNI)
            self.assertIsNotNone(st.get("o-ok"))

    def test_normalised_forms_are_accepted(self):
        """`eth_usdt` / `ETHUSDT` 是同一个标的，不该被当成越界（归一后比较）。"""
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            for i, raw in enumerate(("eth_usdt", "ETHUSDT")):
                st.create({"symbol": raw, "order_id": f"o-{i}"}, universe=self.UNI)
                self.assertIsNotNone(st.get(f"o-{i}"), raw)

    def test_empty_symbol_is_not_out_of_universe(self):
        """标的留空 = 「判不出来」，不是「写错」—— 调用方靠这一点保留原有语义。"""
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            st.create({"symbol": "", "order_id": "o-nosym"}, universe=self.UNI)
            self.assertIsNotNone(st.get("o-nosym"))

    def test_without_universe_behaviour_unchanged(self):
        """不给宇宙 → 不校验（旧调用方逐字不变）。"""
        with tempfile.TemporaryDirectory() as td:
            st = SharedOrderStore(Path(td))
            st.create({"symbol": "SOL_USDT", "order_id": "o-any"})
            self.assertIsNotNone(st.get("o-any"))


if __name__ == "__main__":
    unittest.main()
