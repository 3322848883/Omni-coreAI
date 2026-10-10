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

    def test_empty_context_does_not_claim_no_position(self):
        """空订单记忆**不能**说「当前无持仓」—— 那是关于持仓的断言，而这一段只知道订单。

        实盘实测：brooks-btc 持 -56 BTC 时 prompt 里照样写着「（当前无持仓）」——
        一句错话，模型只能靠契约规则 16 去仲裁。现在改成如实说明并指向权威来源。
        """
        txt = _format_order_context(None)
        self.assertNotIn("无持仓", txt)
        self.assertIn("position_state", txt, "要指向权威来源（快照）")
        self.assertEqual(_format_order_context({"orders": []}), txt, "容器形态同样处理")


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


class TestSingleBotWritesOrderMemory(unittest.TestCase):
    """T14 / D-7：单 bot **也写**订单库 —— 否则 `[订单上下文]` 永远是空的。

    此前 `SharedOrderStore` 全仓只有一个写入方（`persona/runner.py`），于是 `plan-loop`
    的 `_order_context_for` 恒为 None：模型每轮看不到**订单级**的 lifecycle /
    recent_events / 自己声明的前提失效价（轮级记忆走 journal，不受影响）。
    """

    @staticmethod
    def _chip(sym="BTC_USDT", inv=None, reason="破位走人"):
        return SimpleNamespace(symbol=sym, invalidation=inv, reasoning=reason)

    @staticmethod
    def _plan(chips=(), reasoning="因为是区间上沿", refs=("c-0",)):
        return SimpleNamespace(cycle_id="c1", reasoning=reasoning,
                               memory_refs=list(refs), chips=list(chips))

    def _open(self, root, runner, sym="BTC_USDT", side="long", inv=81000.0):
        runner._sync_order_memory(root, "b1", self._plan([self._chip(sym, inv)]),
                                  [{"symbol": sym, "action": f"open_{side}", "size_usd": 100,
                                    "tp": 90000.0, "sl": 80000.0}])

    def test_open_creates_record_and_context_becomes_visible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            self.assertIsNone(r._order_context_for(root, "b1", via_group=False),
                              "写之前应当取不到（这正是要修的缺口）")
            self._open(root, r)
            recs = SharedOrderStore(root).list_open()
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["target_account"], "b1", "归属按 bot 记账，单 bot 才取得到")
            self.assertEqual(rec["side"], "long")
            self.assertEqual(rec["symbol"], "BTC_USDT")
            self.assertEqual(rec["premise_invalidation"]["price"], 81000.0)
            self.assertEqual(rec["reason_text"], "因为是区间上沿")
            self.assertIn("c-0", rec["memory_refs"])
            self.assertTrue([e for e in rec["lifecycle"] if e["act"] == "open"])
            self.assertTrue(rec["recent_events"], "recent_events 要跟着刷新")
            self.assertIsNotNone(r._order_context_for(root, "b1", via_group=False),
                                 "写了记录之后必须能取到订单上下文")

    def test_reversal_starts_new_record(self):
        """方向反转**另起一张**（旧单关闭）—— 否则注入的持仓方向与账户相反。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            self._open(root, r, side="long")
            old = SharedOrderStore(root).list_open()[0]["order_id"]
            self._open(root, r, side="short")
            opens = SharedOrderStore(root).list_open()
            self.assertEqual(len(opens), 1)
            self.assertEqual(opens[0]["side"], "short")
            self.assertNotEqual(opens[0]["order_id"], old)
            self.assertEqual(SharedOrderStore(root).get(old)["status"], "closed")

    def test_close_retires_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            self._open(root, r)
            r._sync_order_memory(root, "b1", self._plan(),
                                 [{"symbol": "BTC_USDT", "action": "close"}])
            self.assertEqual(SharedOrderStore(root).list_open(), [])
            self.assertIsNone(r._order_context_for(root, "b1", via_group=False))

    def test_multi_symbol_writes_one_record_each(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT", "ETH_USDT"])
            r._sync_order_memory(
                root, "b1",
                self._plan([self._chip("BTC_USDT", 81000.0), self._chip("ETH_USDT", 2400.0)]),
                [{"symbol": "BTC_USDT", "action": "open_long", "size_usd": 50},
                 {"symbol": "ETH_USDT", "action": "open_short", "size_usd": 50}])
            recs = {r_["symbol"]: r_ for r_ in SharedOrderStore(root).list_open()}
            self.assertEqual(sorted(recs), ["BTC_USDT", "ETH_USDT"])
            self.assertEqual(recs["ETH_USDT"]["side"], "short")

    def test_modify_updates_prices_without_new_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            self._open(root, r)
            oid = SharedOrderStore(root).list_open()[0]["order_id"]
            r._sync_order_memory(root, "b1", self._plan(),
                                 [{"symbol": "BTC_USDT", "action": "modify_tp_sl",
                                   "tp": 88000.0, "sl": 82000.0}])
            recs = SharedOrderStore(root).list_open()
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["order_id"], oid)
            self.assertEqual(recs[0]["sl"], 82000.0)
            self.assertTrue([e for e in recs[0]["lifecycle"] if e["act"] == "modify_sl"])

    def test_unknown_action_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            r._sync_order_memory(root, "b1", self._plan(),
                                 [{"symbol": "BTC_USDT", "action": "hold"}])
            self.assertEqual(SharedOrderStore(root).list_open(), [])

    def test_failure_never_breaks_the_cycle(self):
        """记忆是增益：写库炸了只能告警，不能影响下单。"""
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            with mock.patch.object(SharedOrderStore, "create",
                                   side_effect=RuntimeError("disk full")):
                self._open(root, r)      # 不应抛出

    # ── 管理动作也要建记录（实盘部署后踩到的 case）──────────────────

    @staticmethod
    def _snap(state="position_open", size=-56, entry="82790"):
        return {"account": {"position_state": {"BTC_USDT": state},
                            "positions": ([{"contract": "BTC_USDT", "size": size,
                                            "entry_price": entry}] if size else [])}}

    def test_manage_action_creates_record_from_position(self):
        """**实盘踩到的那一个**：持仓中的 bot 只发 `modify_tp_sl`（没有 open 动作）。

        只在 `open_*` 时建记录的话，这类 bot 的订单记忆**永远为空** —— 部署到实盘后实测：
        `brooks-btc` 每轮 `exec=True orders=1`，而订单库一条都没有。而 `modify_tp_sl`
        本身不带 `side`，方向只能从快照的持仓推。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            r._sync_order_memory(
                root, "b1", self._plan([self._chip("BTC_USDT", 83000.0)]),
                [{"symbol": "BTC_USDT", "action": "modify_tp_sl", "sl": 83500.0}],
                self._snap())
            opens = SharedOrderStore(root).list_open()
            self.assertEqual(len(opens), 1, "管理动作必须也能建出记录")
            self.assertEqual(opens[0]["side"], "short", "方向从持仓推（size<0 → 空）")
            self.assertEqual(opens[0]["entry_price"], "82790")
            self.assertEqual(opens[0]["sl"], 83500.0)
            self.assertTrue([e for e in opens[0]["lifecycle"] if e["act"] == "modify_sl"])
            self.assertIsNotNone(r._order_context_for(root, "b1", via_group=False),
                                 "这正是实盘缺的那一段上下文")

    def test_manage_action_with_long_position(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            r._sync_order_memory(root, "b1", self._plan(), [
                {"symbol": "BTC_USDT", "action": "modify_tp_sl", "tp": 90000.0}],
                self._snap(size=56))
            self.assertEqual(SharedOrderStore(root).list_open()[0]["side"], "long")

    def test_manage_action_without_position_does_not_invent(self):
        """推不出方向 → **不建**（不猜）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            r._sync_order_memory(root, "b1", self._plan(), [
                {"symbol": "BTC_USDT", "action": "modify_tp_sl", "sl": 1.0}],
                self._snap(state="flat", size=0))
            self.assertEqual(SharedOrderStore(root).list_open(), [])

    def test_close_without_record_creates_nothing(self):
        """本来就没记录 → 不为一次平仓造一条（省掉一堆只有 open+close 的空记录）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            r._sync_order_memory(root, "b1", self._plan(),
                                 [{"symbol": "BTC_USDT", "action": "close"}],
                                 self._snap())
            self.assertEqual(SharedOrderStore(root).list_open(), [])

    def test_modify_then_reuse_same_record(self):
        """第二轮管理动作复用同一条记录（不是每轮新建）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            acts = [{"symbol": "BTC_USDT", "action": "modify_tp_sl", "sl": 83500.0},
                    {"symbol": "BTC_USDT", "action": "modify_tp_sl", "sl": 83800.0}]
            for o in acts:
                r._sync_order_memory(root, "b1", self._plan(), [o], self._snap())
            opens = SharedOrderStore(root).list_open()
            self.assertEqual(len(opens), 1)
            self.assertEqual(opens[0]["sl"], 83800.0, "第二次的价要覆盖第一次")
            self.assertEqual(len([e for e in opens[0]["lifecycle"]
                                  if e["act"] == "modify_sl"]), 2, "两次事件都要留痕")

    # ── 过期记录回收（快照的 position_state 是唯一判据）──────────────

    def _sync_state(self, root, runner, state):
        """跑一轮「没下单」的同步，并带上快照里的持仓状态。"""
        runner._sync_order_memory(root, "b1", self._plan(), [],
                                  {"account": {"position_state": {"BTC_USDT": state}}})

    def test_flat_retires_stale_record(self):
        """交易所侧 SL/TP 触发平仓**没有信号** → 记录不会自己关，靠快照的 flat 回收。

        不回收的后果：下一轮 prompt 里挂着一个**已经不存在的仓位**。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            self._open(root, r)
            oid = SharedOrderStore(root).list_open()[0]["order_id"]
            self._sync_state(root, r, "flat")
            self.assertEqual(SharedOrderStore(root).list_open(), [])
            rec = SharedOrderStore(root).get(oid)
            self.assertEqual(rec["status"], "closed")
            self.assertTrue([e for e in rec["lifecycle"] if e["detail"] == "stale_flat"])

    def test_entry_pending_must_not_be_retired(self):
        """**最关键的一条**：待成交的突破单正是记录的正常来源。

        实测 ab-multi-new 那张 `stop_entry_long`（XAU 挂单未成交）就落在这个状态；
        若把它当过期回收，模型会丢掉自己挂的单 —— 比没有记忆更糟。
        （`flat` 在 snapshot.py 的定义是「无持仓**且**无待成交入场单」，两者靠这个区分。）
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            self._open(root, r)
            self._sync_state(root, r, "entry_pending")
            self.assertEqual(len(SharedOrderStore(root).list_open()), 1)

    def test_position_open_is_not_retired(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            self._open(root, r)
            self._sync_state(root, r, "position_open")
            self.assertEqual(len(SharedOrderStore(root).list_open()), 1)

    def test_unknown_and_missing_state_are_not_retired(self):
        """`unknown`（持仓取数失败）与缺键一律**不动** —— 宁可留一条过期记录，
        也不要因为读不到就抹掉证据（与规则 14/16 对 unknown 的处理同源）。"""
        for snap in ({"account": {"position_state": {"BTC_USDT": "unknown"}}},
                     {"account": {"position_state": {}}},
                     {"account": {}},
                     {}):
            with self.subTest(snap=snap):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    r = _loop_runner(["BTC_USDT"])
                    self._open(root, r)
                    r._sync_order_memory(root, "b1", self._plan(), [], snap)
                    self.assertEqual(len(SharedOrderStore(root).list_open()), 1)

    def test_pending_inbox_signal_blocks_retirement(self):
        """**时序闸**：plan-loop 只写信号，挂单的是 `run`（轮询 ~2s）。

        在它消费之前账户上什么都没有 —— 若不看这一点，就会把**刚挂出去、还没落地**的订单
        当成过期回收（本地实测：没有 `run` 进程时第一轮就被误回收）。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            r.inbox = root / "inbox"
            r.inbox.mkdir(parents=True)
            self._open(root, r)
            (r.inbox / "sig.json").write_text("{}", encoding="utf-8")   # 未被消费
            self._sync_state(root, r, "flat")
            self.assertEqual(len(SharedOrderStore(root).list_open()), 1,
                             "inbox 还有未消费的信号 → 不回收")

    def test_empty_inbox_still_retires(self):
        """inbox 空（信号已被消费）→ 该回收还是要回收。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            r.inbox = root / "inbox"
            r.inbox.mkdir(parents=True)
            self._open(root, r)
            self._sync_state(root, r, "flat")
            self.assertEqual(SharedOrderStore(root).list_open(), [])

    def test_without_snapshot_behaviour_unchanged(self):
        """不给快照 → 不回收（旧调用方逐字不变）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = _loop_runner(["BTC_USDT"])
            self._open(root, r)
            r._sync_order_memory(root, "b1", self._plan(), [])
            self.assertEqual(len(SharedOrderStore(root).list_open()), 1)


if __name__ == "__main__":
    unittest.main()
