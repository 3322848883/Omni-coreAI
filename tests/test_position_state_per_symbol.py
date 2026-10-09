# -*- coding: utf-8 -*-
"""账户快照必须**按币分区**（T5-a，见 docs/compose/spec/symbol-as-parameter.md [S2.4③]）。

背景（证据 B-1）：`account.position_state` 原先只有一个**账户级**值 ——
- 持 BTC 空 ETH 时，规则 16/17 会把「账户里有仓」读成「这个币有仓」，于是对 ETH 发
  `modify_tp_sl` → `NO_POSITION`，白烧一轮；
- 反向（只有 ETH 有孤儿保护单、BTC 有仓）时，账户级值是 `position_open`，规则 14
  永不触发 → 孤儿单静默留存。

修法：`position_state` 改成 `{symbol: state}`，按 chip 的 symbol 取值；旧单值保留在
`position_state_any`（值 = 「任一币有仓」）做一版兼容。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from omnialpha.strategist.prompt import SYSTEM_PROMPT
from omnialpha.strategist.snapshot import collect_snapshot, position_state

BTC_POS = {"contract": "BTC_USDT", "size": 19, "entry_price": "84000", "mode": "dual", "leverage": 20}
ETH_POS = {"contract": "ETH_USDT", "size": -7, "entry_price": "3200", "mode": "dual", "leverage": 20}
ETH_OPEN_ORDER = {
    "id": "o-eth", "contract": "ETH_USDT", "size": 5, "left": 5, "price": "3200",
    "status": "open", "text": "t-brk", "tif": "gtc", "is_reduce_only": False,
}
ETH_ORPHAN_PROTECTION = {
    "id": "po-eth-sl", "status": "open", "order_type": "price", "direction": "short",
    "contract": "ETH_USDT",
    "initial": {"size": -5, "is_reduce_only": True, "text": "t-brk-sl"},
    "trigger": {"price": "3100.0", "rule": 2},
}


class _Client:
    """最小交易所客户端（只提供 collect_snapshot 需要的接口，不联网）。"""

    def __init__(self, positions=None, orders=None, price_orders=None):
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self._price_orders = list(price_orders or [])

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
        # `available` 必须非空，否则 snapshot 会走 account["error"] 分支、跳过持仓段
        return {"available": "100.0", "total": "100.0", "position_mode": "dual"}

    def get_positions(self):
        return self._positions

    def list_orders(self):
        return self._orders

    def list_price_orders(self, sym):
        return [p for p in self._price_orders if p.get("contract") == sym]


def _snap(symbols, **kw) -> dict:
    return collect_snapshot(_Client(**kw), list(symbols), candles=5, interval="15m")


class TestPositionStateIsPerSymbol(unittest.TestCase):
    def test_state_is_keyed_by_symbol(self):
        """持 BTC 空 ETH：两个币的状态必须**分开**报，不能共用一个账户级值。"""
        snap = _snap(["BTC_USDT", "ETH_USDT"], positions=[BTC_POS])
        ps = snap["account"]["position_state"]
        self.assertIsInstance(ps, dict, f"position_state 必须按币分区：{ps!r}")
        self.assertEqual(ps["BTC_USDT"], "position_open")
        self.assertEqual(ps["ETH_USDT"], "flat",
                         "ETH 无仓，不该被 BTC 的持仓带成 position_open")

    def test_note_is_keyed_by_symbol(self):
        snap = _snap(["BTC_USDT", "ETH_USDT"], positions=[BTC_POS])
        notes = snap["account"]["position_state_note"]
        self.assertIsInstance(notes, dict)
        self.assertEqual(set(notes), set(snap["account"]["position_state"]))
        self.assertTrue(all(notes.values()), "每个币都要有说明")

    def test_entry_pending_is_per_symbol(self):
        """ETH 有未成交入场单、BTC 有仓 —— ETH 必须是 entry_pending。"""
        snap = _snap(["BTC_USDT", "ETH_USDT"], positions=[BTC_POS], orders=[ETH_OPEN_ORDER])
        ps = snap["account"]["position_state"]
        self.assertEqual(ps["ETH_USDT"], "entry_pending")
        self.assertEqual(ps["BTC_USDT"], "position_open")

    def test_orphan_protection_on_flat_symbol_is_flat(self):
        """B-1 的反向：只有 ETH 有孤儿保护单而 BTC 有仓 → ETH 必须是 flat。

        否则规则 14（孤儿保护单必须撤）永远看不到触发条件，孤儿单静默留存。
        """
        snap = _snap(["BTC_USDT", "ETH_USDT"], positions=[BTC_POS],
                     price_orders=[ETH_ORPHAN_PROTECTION])
        self.assertEqual(snap["account"]["position_state"]["ETH_USDT"], "flat")
        self.assertEqual(snap["account"]["position_state_any"], "position_open")

    def test_unknown_is_per_symbol(self):
        """账户取数失败 → 每个币都是 unknown（不得报成 flat）。"""
        class _Broken(_Client):
            def get_account(self):
                raise RuntimeError("public get failed")

        snap = collect_snapshot(_Broken(), ["BTC_USDT", "ETH_USDT"], candles=5, interval="15m")
        ps = snap["account"]["position_state"]
        self.assertEqual(set(ps.values()), {"unknown"}, f"取不到与取到空必须分开：{ps}")

    def test_keys_cover_universe_and_observed_symbols(self):
        """宇宙外的币也要有键 —— 账户可能是共享的，漏报某币有仓更危险。"""
        snap = _snap(["ETH_USDT"], positions=[BTC_POS])
        self.assertIn("ETH_USDT", snap["account"]["position_state"])
        self.assertIn("BTC_USDT", snap["account"]["position_state"])


class TestLegacyAccountLevelStateKept(unittest.TestCase):
    """旧单值必须保留一版（`position_state_any` = 「任一币有仓」）—— 向后兼容。"""

    def test_any_keeps_old_semantics(self):
        snap = _snap(["BTC_USDT", "ETH_USDT"], positions=[BTC_POS])
        self.assertEqual(snap["account"]["position_state_any"], "position_open")
        self.assertIsInstance(snap["account"]["position_state_note_any"], str)

    def test_any_is_flat_when_nothing_at_all(self):
        snap = _snap(["BTC_USDT"])
        self.assertEqual(snap["account"]["position_state_any"], "flat")

    def test_any_ignores_universe(self):
        """「任一币有仓」按**账户**算，不按宇宙算。"""
        snap = _snap(["ETH_USDT"], positions=[BTC_POS])
        self.assertEqual(snap["account"]["position_state_any"], "position_open")
        self.assertEqual(snap["account"]["position_state"]["ETH_USDT"], "flat")


class TestClassifierTakesSymbols(unittest.TestCase):
    """分类函数本身必须能按 symbols 过滤（约 :146-152 那段计数）。"""

    ACC = {"positions": [{"contract": "BTC_USDT", "size": 19}], "open_orders": []}

    def test_filters_positions_by_symbols(self):
        st, _ = position_state(self.ACC, ["ETH_USDT"])
        self.assertEqual(st, "flat", "按 ETH 看，BTC 的持仓不该算数")

    def test_no_symbols_keeps_old_behaviour(self):
        st, _ = position_state(self.ACC)
        self.assertEqual(st, "position_open")

    def test_filters_open_orders_by_symbols(self):
        acc = {"positions": [], "open_orders": [
            {"contract": "ETH_USDT", "status": "open"},
        ]}
        self.assertEqual(position_state(acc, ["ETH_USDT"])[0], "entry_pending")
        self.assertEqual(position_state(acc, ["BTC_USDT"])[0], "flat")

    def test_error_state_is_not_per_symbol(self):
        """取数失败与币无关（一个接口故障影响所有币），按币过滤也必须是 unknown。"""
        self.assertEqual(position_state({"error": "down"}, ["ETH_USDT"])[0], "unknown")


class TestBySymbolGrouping(unittest.TestCase):
    """`positions_by_symbol` / `open_orders_by_symbol`：把「忽略别的 bot 的单」
    从提示词自律**下沉为字段**（每行带 `in_universe`）。"""

    def test_positions_grouped_with_in_universe(self):
        snap = _snap(["ETH_USDT"], positions=[BTC_POS, ETH_POS])
        grouped = snap["account"]["positions_by_symbol"]
        self.assertEqual(set(grouped), {"BTC_USDT", "ETH_USDT"})
        self.assertIs(grouped["BTC_USDT"][0]["in_universe"], False,
                      "宇宙外的持仓必须显式标 False —— 提示词规则 19 靠它落地")
        self.assertIs(grouped["ETH_USDT"][0]["in_universe"], True)
        self.assertEqual(grouped["ETH_USDT"][0]["size"], -7)

    def test_open_orders_grouped_with_in_universe(self):
        snap = _snap(["BTC_USDT"], orders=[ETH_OPEN_ORDER])
        grouped = snap["account"]["open_orders_by_symbol"]
        self.assertIs(grouped["ETH_USDT"][0]["in_universe"], False)

    def test_flat_rows_also_carry_flag(self):
        snap = _snap(["BTC_USDT"], positions=[BTC_POS], orders=[ETH_OPEN_ORDER])
        acct = snap["account"]
        self.assertIs(acct["positions"][0]["in_universe"], True)
        self.assertIs(acct["open_orders"][0]["in_universe"], False)

    def test_protections_carry_flag(self):
        """保护单只对「宇宙 ∪ 有持仓的币」查（本来就是按币查的），
        所以 `in_universe=False` 出现在「有仓但不在宇宙」的币上。"""
        snap = _snap(["BTC_USDT"], positions=[ETH_POS], price_orders=[ETH_ORPHAN_PROTECTION])
        self.assertIs(snap["account"]["protections"][0]["in_universe"], False)

    def test_no_row_is_dropped(self):
        """分组只做投影，不许丢行（本仓反复踩「静默丢维度」）。"""
        snap = _snap(["BTC_USDT"], positions=[BTC_POS, ETH_POS])
        flat = len(snap["account"]["positions"])
        self.assertEqual(sum(len(v) for v in snap["account"]["positions_by_symbol"].values()), flat)


class TestContractRulesArePerSymbol(unittest.TestCase):
    """规则 14/16/17 必须按 chip 的 symbol 取状态（T5-c）。

    光把快照改成按币分区还不够：契约仍写「以 `account.position_state` 为准」的话，
    模型会把**账户级**值（= 任一币有仓）当成该币有仓，B-1 的误放行照旧。
    """

    @staticmethod
    def _rule(n: int) -> str:
        start = SYSTEM_PROMPT.index(f"\n{n}) ")
        try:
            end = SYSTEM_PROMPT.index(f"\n{n + 1}) ")
        except ValueError:
            end = len(SYSTEM_PROMPT)
        return SYSTEM_PROMPT[start:end]

    def test_each_rule_points_at_the_symbol_key(self):
        for n in (14, 16, 17):
            with self.subTest(rule=n):
                self.assertIn("position_state[symbol]", self._rule(n),
                              f"规则 {n} 没按 chip 的 symbol 取状态")

    def test_fallback_to_account_level_is_documented(self):
        for n in (14, 16, 17):
            with self.subTest(rule=n):
                self.assertIn("position_state_any", self._rule(n))

    def test_warns_that_account_level_is_not_this_symbol(self):
        """必须让模型理解「账户级值 ≠ 该币有仓」—— 否则 B-1 的误判会重演。"""
        self.assertIn("账户级值 ≠ 该币有仓", self._rule(16))

    def test_unknown_exception_is_scoped_to_that_symbol(self):
        """规则 14 的例外必须挂在「该币」的 unknown 上，而不是账户级 unknown。"""
        self.assertIn("该币", self._rule(14))
        self.assertIn("禁止撤销任何 tp/sl 保护单", self._rule(14))


if __name__ == "__main__":
    unittest.main()
