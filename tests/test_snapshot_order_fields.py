# -*- coding: utf-8 -*-
"""快照里的挂单必须能**自证身份**（限价/市价、入场条件单、预挂保护单）。

背景（实盘 2026-10-06）：执行器把 `stop_entry_*` 下的**入场条件单**和 TP/SL
**保护单**一起挂在交易所的条件单列表里（`executor.py` 的 `hang_mode: simultaneous`
是刻意设计——成交即生效、零裸仓窗口）。而 `snapshot.py` 的 `protections` 原先只取
6 个字段（contract/id/size/trigger_price/rule/text/status），两种单形状完全一样，
AI 只能靠 `text` 后缀猜。

实测后果：那一轮 AI 在推理里花了上千字猜「85850 那个单是 stop 还是 limit」，
而 `trades.jsonl` 里 `"order_type": "limit"` 写得清清楚楚。

更糟的是它与人格第 25 条（孤儿保护单必须撤）叠加：`entry_pending` 状态恰好是
「无持仓 + 有保护单」，AI 分不清预挂保护与真孤儿单，于是撤掉→重挂→再撤，
连续两轮 `cancel_price_all,stop_entry_long`（cycle 216/217，相隔 1 分钟）。

同一模式已发生过一次：`snapshot.py` 的注释记录 2026-10-04 AI 指出
「their trigger prices are null in the data」。

修法：**只加字段、不删字段**，两种形状（Gate 嵌 initial/trigger、paper 扁平库行）都要认。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from omnialpha.strategist.snapshot import (_flag, _order_kind, collect_snapshot,
                                           position_state)


class _Client:
    """最小交易所客户端：只提供 collect_snapshot 需要的接口，不联网。"""

    def __init__(self, orders=None, price_orders=None):
        self._orders = list(orders or [])
        self._price_orders = list(price_orders or [])

    def public_get(self, path, qs=""):
        return [[1000, "1", "1", "1", "1", "1", "0"]]

    def get_ticker(self, sym):
        return {"last": "84000"}

    def get_last_price(self, sym):
        return 84000.0

    def get_contract(self, sym):
        return SimpleNamespace(quanto_multiplier=0.0001, order_size_round=0,
                               order_price_round=0.1, leverage_max=20)

    def get_contract_stats(self, sym, limit=1):
        return []

    def get_orderbook_top(self, sym, limit=5):
        return {"bids": [], "asks": []}

    def get_account(self):
        # `available` 必须非空：snapshot.py 把它当账户健康的判据，缺了会走
        # `account["error"]` 分支，后续 positions/open_orders/protections 全被跳过。
        return {"available": "100.0", "total": "100.0", "position_mode": "dual"}

    def get_positions(self):
        return []

    def list_orders(self):
        return self._orders

    def list_price_orders(self, sym):
        return self._price_orders


# ── 条件单的两种形状 ────────────────────────────────────────

# Gate：字段嵌在 initial / trigger 里
GATE_PROTECTION = {
    "id": "po-sl", "status": "open", "order_type": "price", "direction": "short",
    "initial": {"size": -27, "is_reduce_only": True, "text": "t-brk-sl"},
    "trigger": {"price": "85550.0", "rule": 2},
}
GATE_ENTRY_CONDITION = {
    "id": "po-entry", "status": "open", "order_type": "price", "direction": "long",
    "initial": {"size": 27, "is_reduce_only": False, "text": "t-brk"},
    "trigger": {"price": "86690.0", "rule": 1},
}
# paper：`_price_order_view` 是扁平库行，字段直接在顶层。
# **注意它没有 `direction`** —— paper 存的是 `side`（buy/sell，见 `store.py:415`），
# 所以方向必须从 size 符号派生，否则 paper 下这个字段恒为 null。
PAPER_PROTECTION = {
    "id": "po-sl-paper", "status": "open", "order_type": "price",
    "size": -27, "trigger_price": "85550.0", "rule": 2,
    "text": "t-brk-sl", "reduce_only": 1, "side": "sell",
}
# 普通挂单：Gate 的 list_orders() 返回里**没有** order_type，只有 tif
GATE_OPEN_ORDER = {
    "id": "o-1", "contract": "BTC_USDT", "size": 27, "left": 27,
    "price": "85850", "status": "open", "text": "t-brk",
    "tif": "gtc", "is_reduce_only": False,
}


def _snap(**kw) -> dict:
    return collect_snapshot(_Client(**kw), ["BTC_USDT"], candles=5, interval="15m")


class TestProtectionsCarryIdentity(unittest.TestCase):
    """protections 必须能区分「预挂保护单」与「入场条件单」。"""

    def test_gate_shape_reduce_only_flag(self):
        snap = _snap(price_orders=[GATE_PROTECTION, GATE_ENTRY_CONDITION])
        prot = {p["id"]: p for p in snap["account"]["protections"]}
        self.assertIn("po-sl", prot, "保护单没进 protections")
        self.assertIs(prot["po-sl"]["is_reduce_only"], True,
                      "预挂保护单没标 is_reduce_only=true —— AI 无法与入场条件单区分")
        self.assertIs(prot["po-entry"]["is_reduce_only"], False,
                      "入场条件单没标 is_reduce_only=false")

    def test_gate_shape_order_type_and_direction(self):
        snap = _snap(price_orders=[GATE_PROTECTION])
        p = snap["account"]["protections"][0]
        self.assertEqual(p["order_type"], "price")
        self.assertEqual(p["direction"], "short")

    def test_paper_shape_reduce_only_normalized_to_bool(self):
        """paper 存 0/1，必须归一化成 bool —— 否则 AI 看到 1/0 还要再猜一层。"""
        snap = _snap(price_orders=[PAPER_PROTECTION])
        p = snap["account"]["protections"][0]
        self.assertIs(p["is_reduce_only"], True, f"paper 形状没认：{p}")

    def test_paper_shape_direction_derived_from_size(self):
        """paper 的库行**没有** `direction` 字段（它只有 `side`: buy/sell）。

        方向必须从张数符号推出来 —— 否则 paper 下这个字段恒为 `null`，
        而「两种形状都要认」是本条的验收标准。
        """
        snap = _snap(price_orders=[PAPER_PROTECTION])
        p = snap["account"]["protections"][0]
        self.assertEqual(p["direction"], "short",
                         f"paper 形状的方向没从 size 符号派生出来：{p}")

    def test_paper_shape_positive_size_is_long(self):
        entry = dict(PAPER_PROTECTION, id="po-entry-paper", size=27,
                     text="t-brk", reduce_only=0, side="buy")
        snap = _snap(price_orders=[entry])
        p = snap["account"]["protections"][0]
        self.assertEqual(p["direction"], "long")
        self.assertIs(p["is_reduce_only"], False)

    def test_existing_fields_not_lost(self):
        """只加不删：老字段必须原样还在（下游与测试都依赖）。"""
        snap = _snap(price_orders=[GATE_PROTECTION])
        p = snap["account"]["protections"][0]
        for k in ("contract", "id", "size", "trigger_price", "rule", "text", "status"):
            self.assertIn(k, p, f"补字段时弄丢了 {k}")
        self.assertEqual(p["trigger_price"], "85550.0")
        self.assertEqual(p["rule"], 2)


class TestOpenOrdersCarryIdentity(unittest.TestCase):
    """普通挂单没有 order_type，只有 tif —— 至少要能看到 tif。"""

    def test_tif_and_reduce_only_exposed(self):
        snap = _snap(orders=[GATE_OPEN_ORDER])
        o = snap["account"]["open_orders"][0]
        self.assertEqual(o["tif"], "gtc", "普通挂单缺 tif —— 无法判断是限价还是 IOC")
        self.assertIs(o["is_reduce_only"], False)

    def test_existing_fields_not_lost(self):
        snap = _snap(orders=[GATE_OPEN_ORDER])
        o = snap["account"]["open_orders"][0]
        for k in ("contract", "id", "size", "price", "left", "status", "text"):
            self.assertIn(k, o, f"补字段时弄丢了 {k}")


class TestFlagNormalisation(unittest.TestCase):
    """布尔标记必须归一化，且**字符串形式要单独认**。

    `bool("false")` 是 `True`。一旦某个数据源把布尔写成字符串，`is_reduce_only`
    就恒真 —— 而它是人格第 25 条区分「预挂保护」与「入场条件单」的唯一判据，
    判错会把入场单当保护单、或反过来撤掉真保护单。
    """

    def test_string_false_is_false(self):
        for s in ("false", "False", "0", "no", "none", "null", " FALSE "):
            with self.subTest(v=s):
                self.assertIs(_flag(s), False)

    def test_string_true_is_true(self):
        for s in ("true", "1", "yes", " True "):
            with self.subTest(v=s):
                self.assertIs(_flag(s), True)

    def test_empty_string_counts_as_absent(self):
        self.assertIs(_flag("", True), True, "空串应视为没值，继续看下一个")
        self.assertIs(_flag("   ", None), None)

    def test_native_types(self):
        self.assertIs(_flag(1), True)
        self.assertIs(_flag(0), False)
        self.assertIs(_flag(True), True)
        self.assertIs(_flag(None, False), False)
        self.assertIsNone(_flag(None, None))


class TestOrderKindIsDerived(unittest.TestCase):
    """`tif` 必须被翻译成 AI 想知道的词。

    Gate 的 `list_orders()` **不返回 `order_type`**（实测字段清单里没有），只有 `tif`。
    实测 2026-10-07 那轮，模型推理的前 23 行和后 10 行都在猜「84350 是限价单还是
    突破单」，约占整段推理的五分之一 —— 纯属信息缺失导致的空转。
    """

    def test_tif_mapping(self):
        for tif, want in (("gtc", "limit"), ("poc", "post_only"),
                          ("ioc", "ioc"), ("fok", "fok"), ("GTC", "limit")):
            with self.subTest(tif=tif):
                self.assertEqual(_order_kind(tif, "84350"), want)

    def test_zero_price_without_tif_is_market(self):
        self.assertEqual(_order_kind(None, "0"), "market")
        self.assertEqual(_order_kind("", 0), "market")

    def test_unknown_returns_none(self):
        self.assertIsNone(_order_kind(None, "84350"), "没 tif 不该乱猜")
        self.assertIsNone(_order_kind("weird", "84350"))

    def test_exposed_in_open_orders(self):
        snap = _snap(orders=[GATE_OPEN_ORDER])
        o = snap["account"]["open_orders"][0]
        self.assertEqual(o["kind"], "limit", f"open_orders 没带派生的 kind：{o}")


class TestEntryPendingNoteDistinguishesOrderKinds(unittest.TestCase):
    """说明里要点明 open_orders 与 protections 是两类单 —— 否则 AI 还是会猜。"""

    def test_note_names_both_containers(self):
        _, note = position_state({"positions": [], "open_orders": [{"status": "open"}]})
        self.assertIn("open_orders", note)
        self.assertIn("protections", note)


class TestPositionStateNoteTellsApart(unittest.TestCase):
    """entry_pending 的说明必须给出判据，否则人格第 25 条会把预挂保护当孤儿单撤掉。"""

    def test_entry_pending_note_mentions_reduce_only(self):
        _, note = position_state({
            "positions": [],
            "open_orders": [{"status": "open"}],
        })
        self.assertIn("is_reduce_only", note,
                      "entry_pending 说明没给「预挂保护 vs 入场条件单」的判据 —— "
                      "AI 只能靠 text 后缀猜，会与人格第 25 条冲突")

    def test_flat_state_unchanged(self):
        st, _ = position_state({"positions": [], "open_orders": []})
        self.assertEqual(st, "flat")


if __name__ == "__main__":
    unittest.main()
