# -*- coding: utf-8 -*-
"""paper 引擎的订单视图必须与 Gate 语义对齐（2026-10-14 多轮本地实跑发现）。

两个缺口都会让 `env: paper` 的 bot 行为偏离真实交易所：

① `_order_view` 缺 `left` / `is_reduce_only` / `finish_as`
   → `Executor._has_pending_entry` 读 `o.get("left")`，缺字段时按「可能未成交」保守
     返回 True → **只要库里有历史成交单，该 symbol 的孤儿保护单就永远清不掉**。
   实测（`scripts/e2e_local_rounds.py` 6 轮）：全平后 5 条保护单原地不动、
   `orphan_protector` 告警持续发出。

② `list_orders` / `list_price_orders` 不过滤状态
   → 真实 Gate `/orders` 与 `/price_orders` 只返回**未终结**的单，paper 库却是全量台账。
     不过滤时「已撤/已成交」的历史行会被当成还在挂 —— 既误导本地验证，
     也会让 `_plan_shared_sl` 的同价归并把历史单并进来**放大张数**。

**离线**：用 stub feed 直接构造 `PaperExchange`，不联网、不读配置、毫秒级。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.paper.engine import _order_view  # noqa: E402
from omnialpha.paper.exchange import PaperExchange  # noqa: E402

SYM = "BTC_USDT"


class _Meta:
    quanto_multiplier = 1.0
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class _Feed:
    """最小行情源：价格可控，不联网。"""

    name = "fake"

    def __init__(self, last=100.0):
        self.last = float(last)
        self.bid, self.ask = self.last - 0.1, self.last + 0.1

    def get_last_price(self, symbol):
        return self.last

    def get_orderbook_top(self, symbol, limit=5):
        return {"bids": [{"p": self.bid, "s": 10}], "asks": [{"p": self.ask, "s": 10}]}

    def get_ticker(self, symbol):
        return {"last": self.last, "mark_price": self.last, "funding_rate": 0.0001}

    def get_contract(self, symbol):
        return _Meta()

    def get_klines(self, symbol, interval, limit=100):
        return []

    def get_contract_stats(self, symbol, limit=1):
        return []


class TestOrderViewFields(unittest.TestCase):
    def test_filled_order_has_left_zero(self):
        v = _order_view({"order_id": "1", "status": "filled", "size": 3.0,
                         "filled_size": 3.0, "reduce_only": 0})
        self.assertEqual(v["left"], 0.0)
        self.assertEqual(v["finish_as"], "filled")
        self.assertIs(v["is_reduce_only"], False)

    def test_open_order_keeps_signed_remainder(self):
        long_open = _order_view({"order_id": "1", "status": "open",
                                 "size": 5.0, "filled_size": 2.0})
        self.assertEqual(long_open["left"], 3.0)
        short_open = _order_view({"order_id": "2", "status": "open",
                                  "size": -5.0, "filled_size": 2.0})
        self.assertEqual(short_open["left"], -3.0, "卖单 left 必须保持负号（Gate 语义）")

    def test_reduce_only_mirrored(self):
        v = _order_view({"order_id": "1", "status": "open", "size": -1.0,
                         "reduce_only": 1})
        self.assertIs(v["is_reduce_only"], True)

    def test_non_filled_has_no_finish_as(self):
        self.assertIsNone(_order_view({"order_id": "1", "status": "open",
                                       "size": 1.0})["finish_as"])


class _PaperCase(unittest.TestCase):
    """离线 paper 交易所 + 真 Executor。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self._td.name)
        self.feed = _Feed()
        self.ex = PaperExchange(
            env="paper", store_path=tmp / "account.db", feed=self.feed,
            config={"initial_capital": 10000, "leverage": 20},
        )
        self.addCleanup(self.ex.store.close)
        self.executor = Executor(self.ex, label_prefix="pv", root=tmp, bot_id="pv-bot")

    def _open(self, size: int) -> None:
        # **必须带本 bot 前缀**：`_has_pending_entry` 只认 `_text_owned` 的普通单 ——
        # 不带前缀的入场单不会被它看到，那样就测不出「已成交单被当成还在挂」这个 bug。
        o = self.ex.place_order({"contract": SYM, "size": size, "type": "market",
                                 "text": "t-pv-entry"})
        self.assertIn(o.get("status"), ("filled", "finished"), o)

    def _protector(self, text: str, size: int, trigger: float, rule: int) -> None:
        self.ex.place_price_order({
            "initial": {"contract": SYM, "size": size, "price": "0", "tif": "ioc",
                        "reduce_only": True, "text": text},
            "trigger": {"rule": rule, "price_type": 0, "price": str(trigger)},
        })


class TestListFilters(_PaperCase):
    def test_filled_entry_not_listed(self):
        self._open(3)
        texts = [o.get("text") for o in self.ex.list_orders(SYM)]
        self.assertEqual(texts, [], f"已成交单不该出现在 list_orders：{texts}")

    def test_has_pending_entry_false_after_fill(self):
        """核心回归：修前这里是 True（缺 `left`），孤儿清理被永久跳过。"""
        self._open(3)
        self.assertFalse(self.executor._has_pending_entry(SYM))

    def test_active_protector_listed_then_hidden_after_cancel(self):
        self._open(3)
        self._protector("t-pv-sl", -3, 98.0, 2)
        self.assertEqual(len(self.ex.list_price_orders(SYM)), 1)
        pid = self.ex.list_price_orders(SYM)[0]["id"]
        self.ex.cancel_price_order(pid)
        self.assertEqual(self.ex.list_price_orders(SYM), [],
                         "已撤条件单不该继续出现在 list_price_orders")

    def test_open_order_still_listed(self):
        """未成交的挂单必须照常列出（过滤只去掉终结状态）。"""
        self.ex.place_order({"contract": SYM, "size": 3, "type": "limit",
                             "price": "99.0", "text": "t-pv-entry"})
        rows = self.ex.list_orders(SYM)
        self.assertEqual(len(rows), 1, rows)
        self.assertNotEqual(rows[0]["left"], 0.0)


class TestOrphanCleanupWorks(_PaperCase):
    def test_leftover_protector_is_cleaned(self):
        """**核心回归**：平仓后残留的保护单必须被清掉。

        修前 `_order_view` 缺 `left` → `_has_pending_entry` 因库里的**已成交入场单**
        恒判 True → `_cleanup_orphan_protectors` 每次早退返回 []，paper 模式下
        孤儿清理**等于被关掉**（多轮实跑里保护单会累积到远超持仓）。
        """
        self._open(3)
        self.ex.close_position(SYM, side="long", size=0)
        self.assertEqual(self.executor._symbol_positions(SYM), [])
        # 手工留一条孤儿（模拟「SL 已在交易所触发、TP 残留」这类真实形态）
        self._protector("t-pv-tp", -3, 102.0, 1)
        self.assertEqual(len(self.ex.list_price_orders(SYM)), 1)

        cancelled = self.executor._cleanup_orphan_protectors(SYM)
        self.assertEqual(len(cancelled), 1, f"孤儿未被撤：{cancelled}")
        self.assertEqual(self.ex.list_price_orders(SYM), [])

    def test_cleanup_keeps_protector_while_position_open(self):
        """有持仓时不得把保护单当孤儿撤掉（fail-safe）。"""
        self._open(3)
        self._protector("t-pv-sl", -3, 98.0, 2)
        cancelled = self.executor._cleanup_orphan_protectors(SYM)
        self.assertEqual(cancelled, [])
        self.assertEqual(len(self.ex.list_price_orders(SYM)), 1)

    def test_cleanup_skipped_while_entry_pending(self):
        """有待成交入场单时**不得**清保护单（预挂的 TP/SL 不是孤儿）。"""
        self.ex.place_order({"contract": SYM, "size": 3, "type": "limit",
                             "price": "99.0", "text": "t-pv-entry"})
        self._protector("t-pv-sl", -3, 98.0, 2)
        self.assertTrue(self.executor._has_pending_entry(SYM))
        self.assertEqual(self.executor._cleanup_orphan_protectors(SYM), [])
        self.assertEqual(len(self.ex.list_price_orders(SYM)), 1)


if __name__ == "__main__":
    unittest.main()
