"""Executor 契约集成测试（Gate 形状 body / id 字段 / quanto）。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.paper.engine import PaperEngine  # noqa: E402
from gate_bot.paper.store import ORDER_FILLED, PaperStore  # noqa: E402
from gate_bot.paper.validate import PaperReject  # noqa: E402


class FakeMeta:
    quanto_multiplier = 1.0
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class FakeFeed:
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


def paper_env(tmp):
    store = PaperStore(Path(tmp) / "account.db")
    store.init_config({"initial_capital": "10000", "leverage": "20",
                       "fee_rate": "0.0005", "funding_enabled": "1"})
    feed = FakeFeed()
    eng = PaperEngine(store, feed)
    return store, feed, eng


class TestExecutorContract(unittest.TestCase):
    def test_price_order_nested_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = paper_env(tmp)
            try:
                po = eng.place_price_order({
                    "initial": {"contract": "BTC_USDT", "size": -10, "price": "0",
                                "tif": "ioc", "text": "t-x"},
                    "trigger": {"rule": 1, "price_type": 0, "price": "110.0"},
                })
                self.assertEqual(po["contract"], "BTC_USDT")
                self.assertIn("id", po)
                self.assertAlmostEqual(float(po["trigger_price"]), 110.0)
            finally:
                store.close()

    def test_order_returns_id_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = paper_env(tmp)
            try:
                feed.bid, feed.ask = 100.0, 100.0
                o = eng.place_order({"contract": "BTC_USDT", "size": 10,
                                     "price": "0", "tif": "ioc", "text": "t-x"})
                self.assertIn("id", o)
                self.assertTrue(o["id"])
                self.assertEqual(o["text"], "t-x")
            finally:
                store.close()

    def test_market_via_price_zero_ioc(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = paper_env(tmp)
            try:
                feed.bid, feed.ask = 100.0, 100.2
                o = eng.place_order({"contract": "BTC_USDT", "size": 10,
                                     "price": "0", "tif": "ioc"})
                self.assertEqual(o["status"], ORDER_FILLED)
                self.assertAlmostEqual(o["avg_price"], 100.2)
            finally:
                store.close()

    def test_post_only_alias_poc(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = paper_env(tmp)
            try:
                feed.bid, feed.ask = 100.0, 100.2
                with self.assertRaises(PaperReject):
                    eng.place_order({"contract": "BTC_USDT", "size": 10,
                                     "type": "limit", "price": 101.0, "tif": "poc"})
            finally:
                store.close()

    def test_available_recomputed_after_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = paper_env(tmp)
            try:
                feed.bid, feed.ask = 100.0, 100.0
                before = float(store.get_account()["available"])
                eng.place_order({"contract": "BTC_USDT", "size": 100,
                                 "price": "0", "tif": "ioc"})
                after = float(store.get_account()["available"])
                self.assertLess(after, before)
            finally:
                store.close()

    def test_quanto_applied_in_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, feed, eng = paper_env(tmp)
            try:
                class QMeta(FakeMeta):
                    quanto_multiplier = 0.0001

                eng.feed.get_contract = lambda s: QMeta()
                feed.bid, feed.ask = 100.0, 100.0
                feed.last = 100.0
                eng.place_order({"contract": "BTC_USDT", "size": 1000,
                                 "price": "0", "tif": "ioc"})
                feed.last = 110.0
                feed.bid, feed.ask = 110.0, 110.0
                snap = eng.recalc_equity()
                # (110-100)*1000*0.0001 = 1.0
                self.assertAlmostEqual(snap["unrealised"], 1.0, places=6)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
