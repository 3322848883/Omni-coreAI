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

from omnialpha.memory.context import _format_order_context  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
