# -*- coding: utf-8 -*-
"""模拟盘合约乘数（quanto_multiplier）回归测试。

背景：`PaperEngine._quanto()` 原来在 `feed.get_contract()` 抛异常时**静默兜底成 1.0**，
而 quanto 贯穿 notional / fee / margin / realised PnL。BTC_USDT 真实乘数是 **0.0001**，
兜底成 1.0 会让这四项**全部错 10,000 倍**。

实测（2026-10-02）：`wyckoff-paper` 一笔 `role=liquidation` 的强平 fill 被记成
`size=1905 px=84117.1 fee=80,121.54`（= `1905×84117.1×0.0005`，正是 quanto=1），
该 bot 49 笔成交 `realised_pnl` 合计只有 −192，但 **fee 合计 80,378** → 余额 −70,573，
账户被打爆且不可逆。

修复原则：会计路径**失败得响**（抛 PaperReject，宁可不成交也不算错账）；
展示路径按「未知」处理（返回 None → 记 0），绝不拿 1.0 把金额算错 4 个数量级。
"""
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.paper.engine import PaperEngine  # noqa: E402
from omnialpha.paper.risk import RiskEngine  # noqa: E402
from omnialpha.paper.store import PaperStore  # noqa: E402
from omnialpha.paper.validate import PaperReject, validate_and_round_order  # noqa: E402

BTC_QUANTO = 0.0001


class FakeMeta:
    quanto_multiplier = BTC_QUANTO
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class FlakyFeed:
    """可以随时「坏掉」的 feed，模拟 get_contract 不可用。"""

    name = "flaky"

    def __init__(self, quanto=BTC_QUANTO):
        self.quanto = quanto
        self.broken = False
        self.bid = 100.0
        self.ask = 100.2
        self.last = 100.1
        self.funding_rate = 0.0001

    def get_last_price(self, symbol):
        return self.last

    def get_orderbook_top(self, symbol, limit=5):
        return {"bids": [{"p": self.bid, "s": 1000}], "asks": [{"p": self.ask, "s": 1000}]}

    def get_ticker(self, symbol):
        return {"last": self.last, "mark_price": self.last, "funding_rate": self.funding_rate}

    def get_contract(self, symbol):
        if self.broken:
            raise RuntimeError("contract metadata unavailable")
        m = FakeMeta()
        m.quanto_multiplier = self.quanto
        return m

    def get_klines(self, symbol, interval, limit=100):
        return []

    def get_contract_stats(self, symbol, limit=1):
        return []


@contextmanager
def env(tmp, quanto=BTC_QUANTO):
    store = PaperStore(Path(tmp) / "account.db")
    store.init_config({"initial_capital": "10000", "leverage": "20",
                       "fee_rate": "0.0005", "funding_enabled": "0"})
    feed = FlakyFeed(quanto)
    eng = PaperEngine(store, feed)
    try:
        yield store, feed, eng
    finally:
        store.close()


class TestQuantoGuard(unittest.TestCase):
    def test_strict_raises_when_never_seen(self):
        """会计路径：从未取到乘数 → 抛错，绝不返回 1.0。"""
        with tempfile.TemporaryDirectory() as td:
            with env(td) as (store, feed, eng):
                feed.broken = True
                with self.assertRaises(PaperReject):
                    eng._quanto("BTC_USDT")
                with self.assertRaises(PaperReject):
                    eng._quanto("BTC_USDT", strict=True)

    def test_lenient_returns_none_when_never_seen(self):
        """展示路径：返回 None（调用方按未知处理），不是 1.0。"""
        with tempfile.TemporaryDirectory() as td:
            with env(td) as (store, feed, eng):
                feed.broken = True
                self.assertIsNone(eng._quanto("BTC_USDT", strict=False))

    def test_uses_cache_after_feed_breaks(self):
        """取到过真值后 feed 坏掉 → 用缓存值（0.0001），不回退到 1.0。"""
        with tempfile.TemporaryDirectory() as td:
            with env(td) as (store, feed, eng):
                self.assertEqual(eng._quanto("BTC_USDT"), BTC_QUANTO)
                feed.broken = True
                self.assertEqual(eng._quanto("BTC_USDT"), BTC_QUANTO)
                self.assertEqual(eng._quanto("BTC_USDT", strict=False), BTC_QUANTO)

    def test_validate_rejects_missing_quanto(self):
        """合约元数据缺乘数 → 拒单，而不是按 1.0 算。"""
        class NoQuanto:
            order_size_round = 1.0
            order_price_round = 0.1
            leverage_max = 100
            min_notional_usd = 5.0

        with self.assertRaises(PaperReject):
            validate_and_round_order({"size": 1, "type": "market"}, NoQuanto(), 100.0)

        class ZeroQuanto(NoQuanto):
            quanto_multiplier = 0.0

        with self.assertRaises(PaperReject):
            validate_and_round_order({"size": 1, "type": "market"}, ZeroQuanto(), 100.0)


class TestFeeAccounting(unittest.TestCase):
    def test_fee_uses_real_quanto_not_one(self):
        """核心回归：fee 必须用真实乘数，不能是 1.0（差 10,000 倍）。"""
        with tempfile.TemporaryDirectory() as td:
            with env(td) as (store, feed, eng):
                # 1000 张 × 0.0001 × 100 ≈ 10 USDT 名义，过最小名义额
                eng.place_order({"contract": "BTC_USDT", "size": 1000, "type": "market"})
                fills = store.list_fills("BTC_USDT", limit=10)
                self.assertTrue(fills, "应有一笔成交")
                f = fills[-1]
                expected = abs(f["size"]) * BTC_QUANTO * f["price"] * 0.0005
                self.assertAlmostEqual(f["fee"], expected, places=12)
                # 若退回 quanto=1.0，fee 会是 10,000 倍
                self.assertLess(f["fee"], expected * 100)

    def test_liquidation_deferred_when_quanto_unknown(self):
        """强平时取不到乘数 → 延后（不写 fill、不平仓），而不是记一笔 10,000 倍的手续费。"""
        with tempfile.TemporaryDirectory() as td:
            with env(td) as (store, feed, eng):
                store.upsert_position("BTC_USDT", "single", size=100, entry_price=100.0,
                                      leverage=20, margin=500.0, margin_mode="isolated",
                                      realised_pnl=0.0)
                risk = RiskEngine(store, feed, eng)
                bal_before = float((store.get_account() or {}).get("balance") or 0)
                feed.broken = True
                eng._quanto_cache.clear()
                closed = risk.check_liquidations()   # 触发条件由 last/liq 决定，这里只验不炸
                fills = store.list_fills("BTC_USDT", limit=10)
                for f in fills:
                    self.assertNotEqual(f.get("role"), "liquidation")
                self.assertEqual(float((store.get_account() or {}).get("balance") or 0), bal_before)
                self.assertIsInstance(closed, list)

    def test_equity_degrades_when_quanto_unknown(self):
        """展示路径：乘数未知 → 未实现盈亏记 0（不是 10,000 倍错值）。"""
        with tempfile.TemporaryDirectory() as td:
            with env(td) as (store, feed, eng):
                store.upsert_position("BTC_USDT", "single", size=100, entry_price=90.0,
                                      leverage=20, margin=500.0, margin_mode="isolated",
                                      realised_pnl=0.0)
                feed.last = 100.1          # 浮盈应为 (100.1-90)*100*0.0001 = 0.101
                ok = eng.recalc_equity()
                self.assertAlmostEqual(ok["unrealised"], 0.101, places=6)
                feed.broken = True
                eng._quanto_cache.clear()
                degraded = eng.recalc_equity()
                self.assertEqual(degraded["unrealised"], 0.0)


if __name__ == "__main__":
    unittest.main()
