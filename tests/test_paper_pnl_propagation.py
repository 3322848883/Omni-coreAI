# -*- coding: utf-8 -*-
"""paper 平仓的已实现盈亏必须回传给 executor —— 否则画像永远拿不到数据。

背景：`PaperEngine._apply_fill` 算出 `realised` 后只写进 `fills` 表与 `positions` 表，
`place_order` 返回的订单视图里**没有它**。executor 的 `_close` 读的是 `order["pnl"]`
→ 永远 None → `detail["realized_pnl"]` 从不出现。

实测证据：真实模拟盘一笔平仓的成交日志里确实没有 `realized_pnl`；而
`PersonaRunner._exec_facts_of` 正是靠这个字段回连盈亏 → **策略画像永远停在「无数据」**
（即使把目标账户启用了也一样）。所以这个键是整条画像闭环的最后一环。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor
from omnialpha.paper.engine import PaperEngine
from omnialpha.paper.exchange import PaperExchange
from omnialpha.paper.store import PaperStore
from omnialpha.schema import parse_signal

SYMBOL = "BTC_USDT"
QUANTO = 1.0


class FakeMeta:
    quanto_multiplier = QUANTO
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class FakeFeed:
    name = "fake"

    def __init__(self):
        self.bid, self.ask, self.last = 100.0, 100.2, 100.1
        self.funding_rate = 0.0001

    def get_last_price(self, symbol):
        return self.last

    def get_orderbook_top(self, symbol, limit=5):
        return {"bids": [{"p": self.bid, "s": 100}], "asks": [{"p": self.ask, "s": 100}]}

    def get_ticker(self, symbol):
        return {"last": self.last, "mark_price": self.last, "funding_rate": self.funding_rate}

    def get_contract(self, symbol):
        return FakeMeta()

    def get_klines(self, symbol, interval, limit=100):
        return []

    def get_contract_stats(self, symbol, limit=1):
        return []


def _engine(tmp: str) -> tuple[PaperStore, FakeFeed, PaperEngine]:
    store = PaperStore(Path(tmp) / "account.db")
    store.init_config({"initial_capital": "10000", "leverage": "20",
                       "fee_rate": "0.0005", "funding_enabled": "1"})
    feed = FakeFeed()
    return store, feed, PaperEngine(store, feed)


class TestPaperPnlPropagation(unittest.TestCase):
    """引擎层：订单视图必须带上 `pnl`。"""

    def test_close_returns_realised_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = _engine(tmp)
            try:
                eng.place_order({"contract": SYMBOL, "size": 10, "type": "market"})
                # 价格上涨 → 平多应产生正盈亏
                feed.bid, feed.ask, feed.last = 105.0, 105.2, 105.1
                out = eng.place_order({"contract": SYMBOL, "size": -10, "type": "market",
                                       "reduce_only": 1, "text": "close"})
                # (105.0 - 100.2) * 10 * 1.0
                self.assertAlmostEqual(float(out.get("pnl") or 0), 48.0, places=4,
                                       msg=f"平仓订单视图缺 pnl：{out}")
                self.assertAlmostEqual(float(out.get("realised_pnl") or 0), 48.0, places=4)
            finally:
                store.close()

    def test_open_does_not_report_pnl(self):
        """开仓不该报盈亏（否则画像会把开仓记成一笔盈亏为 0 的交易）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = _engine(tmp)
            try:
                out = eng.place_order({"contract": SYMBOL, "size": 10, "type": "market"})
                self.assertIsNone(out.get("pnl"))
            finally:
                store.close()

    def test_apply_fill_returns_float(self):
        """`_apply_fill` 必须把已实现盈亏返回给调用方（原先返回 None）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = _engine(tmp)
            try:
                eng.place_order({"contract": SYMBOL, "size": 10, "type": "market"})
                feed.bid, feed.ask, feed.last = 110.0, 110.2, 110.1
                order = {"order_id": "o-x", "contract": SYMBOL, "size": -10.0,
                         "reduce_only": 1, "filled_size": 0.0, "avg_price": None}
                got = eng._apply_fill(order, price=110.0, size=-10.0, role="taker")
                self.assertIsInstance(got, float)
                self.assertAlmostEqual(got, (110.0 - 100.2) * 10 * QUANTO, places=4)
            finally:
                store.close()


class TestExecutorCloseStepCarriesPnl(unittest.TestCase):
    """执行器层：真实模拟盘上走一遍 `execute_signal`，平仓步骤必须带 realized_pnl。"""

    def test_close_step_detail_has_realized_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed = FakeFeed()
            ex = PaperExchange(env="paper", store_path=root / "account.db", feed=feed,
                               config={"initial_capital": 10000, "leverage": 20})
            executor = Executor(ex, label_prefix="pt", root=root, bot_id="pnl-e2e")
            try:
                ex.place_order({"contract": SYMBOL, "size": 10, "type": "market"})
                feed.bid, feed.ask, feed.last = 105.0, 105.2, 105.1
                sig = parse_signal({"action": "close", "symbol": SYMBOL, "side": "long",
                                    "type": "market", "meta": {"signal_id": "t-pnl"}})
                report = executor.execute_signal(sig)
                step = next(s for s in report.results if s.action == "close")
                self.assertTrue(step.ok, step.error)
                self.assertIsNotNone(
                    step.detail.get("realized_pnl"),
                    f"平仓步骤缺 realized_pnl —— 画像/成交卡片都拿不到：{step.detail}")
                self.assertAlmostEqual(float(step.detail["realized_pnl"]), 48.0, places=3)
            finally:
                ex.store.close()


if __name__ == "__main__":
    unittest.main()
