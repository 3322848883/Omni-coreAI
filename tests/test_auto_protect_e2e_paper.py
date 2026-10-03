# -*- coding: utf-8 -*-
"""P3：auto-protect 对**真实模拟盘交易所**的端到端验证（离线、确定性）。

此前所有 auto-protect 测试都用自造的假客户端，只验到 `Executor` 的内部逻辑。
这里换成真正的 `PaperExchange` —— 请求要过 `PaperEngine` 的
tick / lot / 最小名义 / 价格带 / 触发价合法性校验，并真的落成一张
`reduce_only` 条件单进 `paper_account.db`。

**这正是「testnet 那条验证」要回答的问题**（「算出的 SL 价位交易所认不认」），
而且完全离线、可重复 —— 不必去实盘或 testnet 上构造裸仓。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor
from omnialpha.paper.exchange import PaperExchange

SYMBOL = "BTC_USDT"

class FakeMeta:
    quanto_multiplier = 1.0
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class FakeFeed:
    """最小行情源：价格可控，不联网。"""

    name = "fake"

    def __init__(self):
        self.bid = 100.0
        self.ask = 100.2
        self.last = 100.1
        self.funding_rate = 0.0001

    def get_last_price(self, symbol):
        return self.last

    def get_orderbook_top(self, symbol, limit=5):
        return {"bids": [{"p": self.bid, "s": 10}], "asks": [{"p": self.ask, "s": 10}]}

    def get_ticker(self, symbol):
        return {"last": self.last, "mark_price": self.last, "funding_rate": self.funding_rate}

    def get_contract(self, symbol):
        return FakeMeta()

    def get_klines(self, symbol, interval, limit=100):
        return []

    def get_contract_stats(self, symbol, limit=1):
        return []


def _flat(po: dict) -> dict:
    """统一条件单视图：Gate 是嵌套 `{initial, trigger}`，模拟盘是平铺。两种都认。"""
    init = po.get("initial") or po
    trig = po.get("trigger") or {}
    return {
        "text": str(init.get("text") or po.get("text") or ""),
        "size": int(init.get("size") or po.get("size") or 0),
        "reduce_only": bool(init.get("is_reduce_only") or init.get("reduce_only")
                            or po.get("reduce_only")),
        "trigger_price": float(trig.get("price") or po.get("trigger_price") or 0),
        "rule": int(trig.get("rule") or po.get("rule") or 0),
    }


class TestAutoProtectOnPaperExchange(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        tmp = Path(self._td.name)
        self.feed = FakeFeed()
        self.ex = PaperExchange(
            env="paper", store_path=tmp / "account.db", feed=self.feed,
            config={"initial_capital": 10000, "leverage": 20},
        )
        self.executor = Executor(
            self.ex, label_prefix="pt", root=tmp, bot_id="paper-e2e",
            account_risk={"auto_protect": True, "auto_protect_sl_pct": 2.0},
        )

    def tearDown(self):
        self.ex.store.close()
        self._td.cleanup()

    def _open(self, size: int) -> None:
        o = self.ex.place_order({"contract": SYMBOL, "size": size, "type": "market"})
        # 模拟盘用 "filled"（Gate 用 "finished"）—— 这里两种都接受，避免绑死某个交易所的措辞
        self.assertIn(o.get("status"), ("filled", "finished"), o)

    def _owned_price_orders(self) -> list[dict]:
        return self.ex.list_price_orders(SYMBOL) or []

    # ── 核心：真能挂上，且是 reduce_only 条件单 ──
    def test_naked_long_gets_a_real_reduce_only_sl(self):
        self._open(10)                       # 多仓 10 张
        before = len(self._owned_price_orders())
        out = self.executor.ensure_protection(SYMBOL)

        self.assertIn("placed", out, out)
        self.assertEqual(out["need_size"], 10)
        orders = self._owned_price_orders()
        self.assertEqual(len(orders), before + 1, "应当真的多出一张条件单")

        sl = _flat(orders[-1])
        self.assertTrue(sl["reduce_only"], f"SL 必须 reduce_only: {orders[-1]}")
        self.assertIn("-sl", sl["text"])
        self.assertLess(sl["trigger_price"], self.feed.last, "多单 SL 必须在 mark 下方")
        self.assertEqual(sl["rule"], 2, "多单 SL 应是跌破触发 rule=2")
        self.assertEqual(abs(sl["size"]), 10, "SL 应覆盖全部 10 张")

    def test_naked_short_gets_sl_above_mark(self):
        self._open(-10)
        out = self.executor.ensure_protection(SYMBOL)
        self.assertIn("placed", out, out)
        sl = _flat(self._owned_price_orders()[-1])
        self.assertTrue(sl["reduce_only"])
        self.assertGreater(sl["trigger_price"], self.feed.last, "空单 SL 必须在 mark 上方")
        self.assertEqual(sl["rule"], 1, "空单 SL 应是涨破触发 rule=1")

    def test_second_sweep_is_idempotent(self):
        self._open(10)
        self.executor.ensure_protection(SYMBOL)
        n = len(self._owned_price_orders())
        out = self.executor.ensure_protection(SYMBOL)
        self.assertEqual(out["skipped"], "sl_present")
        self.assertEqual(len(self._owned_price_orders()), n, "不应重复补")

    def test_partial_coverage_places_only_shortfall(self):
        self._open(10)
        # 先挂一张只覆盖 4 张的 SL（模拟 add_* 让持仓变大后的状态）
        self.ex.place_price_order({
            "initial": {"contract": SYMBOL, "size": -4, "price": "0", "tif": "ioc",
                        "reduce_only": True, "text": "t-pt-sl"},
            "trigger": {"rule": 2, "price_type": 0, "price": str(self.feed.last - 5)},
        })
        out = self.executor.ensure_protection(SYMBOL)
        self.assertIn("placed", out, out)
        self.assertEqual(out["covered_size"], 4)
        self.assertEqual(out["need_size"], 6)

    def test_dry_mode_places_nothing_on_real_exchange(self):
        self.executor.account_risk = {"auto_protect": "dry", "auto_protect_sl_pct": 2.0}
        self._open(10)
        before = len(self._owned_price_orders())
        out = self.executor.ensure_protection(SYMBOL)
        self.assertEqual(out["auto_protect"], "dry")
        self.assertNotIn("placed", out)
        self.assertEqual(len(self._owned_price_orders()), before, "dry 模式绝不能下单")


if __name__ == "__main__":
    unittest.main()
