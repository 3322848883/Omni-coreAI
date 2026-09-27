"""本地模拟盘测试：精度校验、撮合、盈亏、强平、资金费率。"""
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.paper.engine import PaperEngine  # noqa: E402
from gate_bot.paper.exchange import PaperExchange  # noqa: E402
from gate_bot.paper.risk import RiskEngine, liquidation_price  # noqa: E402
from gate_bot.paper.store import (  # noqa: E402
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_OPEN,
    PaperStore,
)
from gate_bot.paper.validate import PaperReject, validate_and_round_order  # noqa: E402


class FakeMeta:
    quanto_multiplier = 1.0
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 5.0


class FakeFeed:
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


@contextmanager
def paper_env(tmp):
    store = PaperStore(Path(tmp) / "account.db")
    store.init_config({"initial_capital": "10000", "leverage": "20",
                       "fee_rate": "0.0005", "funding_enabled": "1"})
    feed = FakeFeed()
    eng = PaperEngine(store, feed)
    try:
        yield store, feed, eng
    finally:
        store.close()


class TestValidate(unittest.TestCase):
    def test_rejects_zero_size(self):
        with self.assertRaises(PaperReject):
            validate_and_round_order({"size": 0}, FakeMeta(), 100.0)

    def test_rounds_lot_size(self):
        out = validate_and_round_order(
            {"size": 3.7, "type": "market"}, FakeMeta(), 100.0, available=1e9)
        self.assertEqual(out["size"], 4.0)

    def test_price_band(self):
        with self.assertRaises(PaperReject):
            validate_and_round_order(
                {"size": 1, "type": "limit", "price": 200.0}, FakeMeta(), 100.0,
                price_band_pct=5.0, available=1e9)

    def test_min_notional(self):
        with self.assertRaises(PaperReject):
            validate_and_round_order(
                {"size": 1, "type": "limit", "price": 1.0}, FakeMeta(), 100.0,
                available=1e9)

    def test_leverage_cap(self):
        with self.assertRaises(PaperReject):
            validate_and_round_order(
                {"size": 1, "type": "market"}, FakeMeta(), 100.0,
                leverage=200, leverage_max=100, available=1e9)

    def test_invalid_tif(self):
        with self.assertRaises(PaperReject):
            validate_and_round_order(
                {"size": 1, "type": "market", "tif": "XXX"}, FakeMeta(), 100.0,
                available=1e9)


class TestMatching(unittest.TestCase):
    def test_market_buy_fills_at_ask(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                feed.ask = 100.5
                feed.bid = 100.0
                o = eng.place_order({"contract": "BTC_USDT", "size": 10, "type": "market"})
                self.assertEqual(o["status"], ORDER_FILLED)
                self.assertAlmostEqual(o["avg_price"], 100.5)

    def test_market_sell_fills_at_bid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                feed.bid = 99.5
                feed.ask = 100.0
                o = eng.place_order({"contract": "BTC_USDT", "size": -10, "type": "market"})
                self.assertEqual(o["status"], ORDER_FILLED)
                self.assertAlmostEqual(o["avg_price"], 99.5)

    def test_limit_waits_until_crossed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                feed.bid, feed.ask = 100.0, 100.2
                o = eng.place_order({"contract": "BTC_USDT", "size": 10,
                                     "type": "limit", "price": 99.0})
                self.assertEqual(o["status"], ORDER_OPEN)
                feed.bid, feed.ask = 98.5, 98.7
                o2 = eng.match_order(o["order_id"])
                self.assertEqual(o2["status"], ORDER_FILLED)

    def test_post_only_rejects_taker(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                feed.bid, feed.ask = 100.0, 100.2
                with self.assertRaises(PaperReject):
                    eng.place_order({"contract": "BTC_USDT", "size": 10,
                                     "type": "limit", "price": 101.0, "tif": "PO"})

    def test_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                feed.bid, feed.ask = 100.0, 100.2
                o = eng.place_order({"contract": "BTC_USDT", "size": 10,
                                     "type": "limit", "price": 98.0})
                store.update_order(o["order_id"], status=ORDER_CANCELLED)
                self.assertEqual(store.get_order(o["order_id"])["status"], ORDER_CANCELLED)


class TestPnL(unittest.TestCase):
    def test_long_unrealised_and_realised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                feed.bid, feed.ask = 100.0, 100.0
                eng.place_order({"contract": "BTC_USDT", "size": 10, "type": "market"})
                feed.bid, feed.ask = 110.0, 110.0
                feed.last = 110.0
                snap = eng.recalc_equity()
                self.assertAlmostEqual(snap["unrealised"], 100.0, places=4)
                eng.place_order({"contract": "BTC_USDT", "size": -10, "type": "market"})
                fills = store.list_fills()
                realised = sum(f["realised_pnl"] for f in fills if f["kind"] == "trade")
                self.assertAlmostEqual(realised, 100.0, places=4)

    def test_fee_deducted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                feed.bid, feed.ask = 100.0, 100.0
                eng.place_order({"contract": "BTC_USDT", "size": 10, "type": "market"})
                self.assertGreater(store.total_fees(), 0)
                acct = store.get_account()
                self.assertLess(float(acct["balance"]), 10000)


class TestLiquidation(unittest.TestCase):
    def test_liquidation_price_long(self):
        liq = liquidation_price(entry=100.0, size=1.0, leverage=20,
                                maintenance_margin_rate=0.005)
        self.assertAlmostEqual(liq, 95.5)

    def test_liquidation_price_short(self):
        liq = liquidation_price(entry=100.0, size=-1.0, leverage=20,
                                maintenance_margin_rate=0.005)
        self.assertAlmostEqual(liq, 104.5)

    def test_liquidation_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                risk = RiskEngine(store, feed, eng)
                feed.bid, feed.ask = 100.0, 100.0
                eng.place_order({"contract": "BTC_USDT", "size": 100, "type": "market"})
                feed.bid, feed.ask = 90.0, 90.0
                feed.last = 90.0
                closed = risk.check_liquidations()
                self.assertTrue(closed)
                self.assertEqual(closed[0]["contract"], "BTC_USDT")


class TestFunding(unittest.TestCase):
    def test_funding_settles(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, feed, eng):
                risk = RiskEngine(store, feed, eng)
                feed.bid, feed.ask = 100.0, 100.0
                eng.place_order({"contract": "BTC_USDT", "size": 10, "type": "market"})
                feed.funding_rate = 0.0001
                recs = risk.settle_funding(force=True)
                self.assertTrue(recs)
                self.assertLess(recs[0]["amount"], 0)


class TestExchangeAdapter(unittest.TestCase):
    def test_paper_exchange_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = FakeFeed()
            ex = PaperExchange(
                env="paper",
                store_path=Path(tmp) / "account.db",
                feed=feed,
                config={"initial_capital": 10000, "leverage": 20},
            )
            try:
                acct = ex.get_account()
                self.assertAlmostEqual(float(acct["available"]), 10000)
                o = ex.place_order({"contract": "BTC_USDT", "size": 10, "type": "market"})
                self.assertEqual(o["status"], ORDER_FILLED)
                poss = ex.get_positions()
                self.assertEqual(len(poss), 1)
                self.assertAlmostEqual(poss[0]["size"], 10)
                info = ex.tick()
                self.assertIn("equity", info)
                ex.close_position("BTC_USDT")
                self.assertEqual(len(ex.get_positions()), 0)
            finally:
                ex.store.close()


if __name__ == "__main__":
    unittest.main()
