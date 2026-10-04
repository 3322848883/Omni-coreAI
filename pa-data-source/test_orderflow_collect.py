# -*- coding: utf-8 -*-
"""orderflow_collect 单元测试（全用构造消息，不依赖实时流）。

实时 WS 不可复现，所以这里手工喂「快照 → 增量 → 逐笔」，再调 flush 检查落库。
覆盖：频道分发、订阅报文、序列号断号、撤单归因、tape/footprint/walls 落库。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orderflow_collect import OrderFlowCollector  # noqa: E402


def snap(contract, bids, asks, seq=100):
    return {"channel": "futures.order_book", "event": "all",
            "result": {"contract": contract, "id": seq,
                       "bids": [{"p": str(p), "s": s} for p, s in bids],
                       "asks": [{"p": str(p), "s": s} for p, s in asks]}}


def upd(contract, b=None, a=None, u=101):
    return {"channel": "futures.order_book_update", "event": "update",
            "result": {"s": contract, "U": u - 1, "u": u,
                       "b": [{"p": str(p), "s": s} for p, s in (b or [])],
                       "a": [{"p": str(p), "s": s} for p, s in (a or [])]}}


def trade(contract, price, size):
    return {"channel": "futures.trades", "event": "update",
            "result": [{"contract": contract, "price": str(price), "size": size}]}


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.c = OrderFlowCollector(["BTC_USDT"], os.path.join(self.td.name, "of.db"))

    def tearDown(self):
        self.c.close()
        self.td.cleanup()

    def test_consumes_three_channels(self):
        self.assertTrue(self.c.on_message(snap("BTC_USDT", [(100, 1)], [(101, 1)])))
        self.assertTrue(self.c.on_message(upd("BTC_USDT", b=[(100, 2)])))
        self.assertTrue(self.c.on_message(trade("BTC_USDT", 100, 5)))

    def test_ignores_other_channels(self):
        self.assertFalse(self.c.on_message({"channel": "futures.candlesticks",
                                            "event": "update", "result": {}}))
        self.assertFalse(self.c.on_message("not json"))

    def test_ignores_unknown_contract(self):
        # 未订阅的合约不应污染状态
        self.c.on_message(snap("ETH_USDT", [(100, 1)], [(101, 1)]))
        self.assertEqual(len(self.c.states["BTC_USDT"].book.bids), 0)

    def test_accepts_raw_json_string(self):
        raw = json.dumps(trade("BTC_USDT", 100, 7))
        self.assertTrue(self.c.on_message(raw))
        self.assertEqual(len(self.c.states["BTC_USDT"].trades), 1)

    def test_subscriptions_shape(self):
        subs = self.c.subscriptions()
        self.assertEqual(len(subs), 3)
        chans = {s["channel"] for s in subs}
        self.assertEqual(chans, {"futures.order_book", "futures.order_book_update",
                                 "futures.trades"})
        for s in subs:
            self.assertEqual(s["event"], "subscribe")


class TestSequenceGuard(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.c = OrderFlowCollector(["BTC_USDT"], os.path.join(self.td.name, "of.db"))
        self.c.on_message(snap("BTC_USDT", [(100, 10)], [(101, 10)], seq=100))

    def tearDown(self):
        self.c.close()
        self.td.cleanup()

    def test_gap_marks_resync_and_drops_update(self):
        # 期望 U=101，却收到 U=200 → 断号
        self.c.on_message(upd("BTC_USDT", b=[(100, 99)], u=200))
        st = self.c.states["BTC_USDT"]
        self.assertTrue(st.resync_needed)
        # 本次增量被丢弃，挂单量未被修改
        self.assertEqual(st.book.bids[100.0].size, 10)

    def test_continuous_update_applies(self):
        self.c.on_message(upd("BTC_USDT", b=[(100, 50)], u=101))
        st = self.c.states["BTC_USDT"]
        self.assertFalse(st.resync_needed)
        self.assertEqual(st.book.bids[100.0].size, 50)

    def test_snapshot_clears_resync(self):
        self.c.on_message(upd("BTC_USDT", b=[(100, 1)], u=999))
        self.assertTrue(self.c.states["BTC_USDT"].resync_needed)
        self.c.on_message(snap("BTC_USDT", [(100, 10)], [(101, 10)], seq=1000))
        self.assertFalse(self.c.states["BTC_USDT"].resync_needed)


class TestFlush(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.td.name, "of.db")
        self.c = OrderFlowCollector(["BTC_USDT"], self.db, tape_sec=0, book_sec=0,
                                    footprint_sec=0, default_big=1000.0)
        self.c.on_message(snap("BTC_USDT", [(100.0, 100)], [(101.0, 100)], seq=1))

    def tearDown(self):
        self.c.close()
        self.td.cleanup()

    def _rows(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def test_tape_written_with_direction(self):
        self.c.on_message(trade("BTC_USDT", 100.0, 60))
        self.c.on_message(trade("BTC_USDT", 100.0, -40))
        self.c.flush(now=1000.0)
        rows = self._rows("SELECT buy_size, sell_size, delta FROM of_tape")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][0], 60.0)
        self.assertAlmostEqual(rows[0][1], 40.0)
        self.assertAlmostEqual(rows[0][2], 20.0)

    def test_big_trade_counted_by_threshold(self):
        self.c.on_message(trade("BTC_USDT", 100.0, 5000))
        self.c.on_message(trade("BTC_USDT", 100.0, 10))
        self.c.flush(now=1000.0)
        rows = self._rows("SELECT big_count, big_size, max_trade FROM of_tape")
        self.assertEqual(rows[0][0], 1)
        self.assertAlmostEqual(rows[0][1], 5000.0)
        self.assertAlmostEqual(rows[0][2], 5000.0)

    def test_book_state_written(self):
        self.c.on_message(upd("BTC_USDT", b=[(100.0, 150)], u=2))
        self.c.flush(now=1000.0)
        rows = self._rows("SELECT place_vol, cancel_vol, grade, levels "
                          "FROM of_book_state")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][0], 50.0)      # 100 → 150，新增 50
        self.assertEqual(rows[0][3], 2)

    def test_cancel_attribution_uses_traded_volume(self):
        # 挂单 100 降到 40：同价位成交 20 → 20 被吃、40 撤单
        self.c.on_message(trade("BTC_USDT", 100.0, 20))
        self.c.on_message(upd("BTC_USDT", b=[(100.0, 40)], u=2))
        self.c.flush(now=1000.0)
        rows = self._rows("SELECT cancel_vol, traded_vol FROM of_book_state")
        self.assertAlmostEqual(rows[0][0], 40.0)
        self.assertAlmostEqual(rows[0][1], 20.0)

    def test_footprint_accumulates_by_price(self):
        self.c.on_message(trade("BTC_USDT", 100.02, 10))
        self.c.on_message(trade("BTC_USDT", 100.04, -5))
        self.c.flush(now=1000.0)
        rows = self._rows("SELECT price, buy_size, sell_size FROM of_footprint")
        # 两笔都按 tick=0.1 落到 100.0
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][1], 10.0)
        self.assertAlmostEqual(rows[0][2], 5.0)

    def test_flush_clears_pending_between_rounds(self):
        self.c.on_message(trade("BTC_USDT", 100.0, 10))
        self.c.flush(now=1000.0)
        self.c.flush(now=1001.0)
        # 第二轮无成交 → 只有一行 tape
        self.assertEqual(len(self._rows("SELECT * FROM of_tape")), 1)

    def test_no_trades_still_clears_traded_by_price(self):
        # 否则撤单归因会误用上一轮的成交量
        st = self.c.states["BTC_USDT"]
        self.c.on_message(trade("BTC_USDT", 100.0, 20))
        self.c.flush(now=1000.0)
        self.assertEqual(st.traded_by_price, {})

    def test_book_state_each_round(self):
        self.c.flush(now=1000.0)
        self.c.flush(now=1001.0)
        self.assertEqual(len(self._rows("SELECT * FROM of_book_state")), 2)


class TestBigThreshold(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.c = OrderFlowCollector(["BTC_USDT"], os.path.join(self.td.name, "of.db"))
        self.st = self.c.states["BTC_USDT"]

    def tearDown(self):
        self.c.close()
        self.td.cleanup()

    def test_default_when_sample_small(self):
        self.assertAlmostEqual(self.st.big_threshold(now=100.0), 1000.0)

    def test_percentile_when_enough_samples(self):
        import time as _t
        now = _t.time()
        for i in range(200):
            self.st.trade_hist.append((now, float(i + 1)))
        thr = self.st.big_threshold(now=now)
        # 200 个样本取 90 分位 → 约 180
        self.assertGreater(thr, 150.0)
        self.assertLess(thr, 200.0)


if __name__ == "__main__":
    unittest.main()
