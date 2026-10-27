# -*- coding: utf-8 -*-
"""v2 写入器（pa-data-source/writer_v2.py）。

最关键的一条：**重写同一根 bar 不能清空指标列**（旧的 INSERT OR REPLACE 会）。
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

import indicators_core as core  # noqa: E402
import schema_v2 as sv2  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402
from writer_v2 import (Bar, SeriesWriter, interval_seconds, should_write_unclosed,  # noqa: E402
                       upsert_bar)

SPECS = parse_indicators([{"ema": [20]}, {"atr": [14]}])
COLS = [c for s in SPECS for c in s.columns]


def fresh_db():
    conn = sqlite3.connect(":memory:")
    sv2.init_v2(conn)
    sv2.ensure_indicator_columns(conn, SPECS)
    return conn


def bar(t=1_700_000_000, c=2400.0, closed=True, src="ws"):
    return Bar(t=t, o=c, h=c + 1, l=c - 1, c=c, v=10.0, closed=closed, src=src)


class TestUpsert(unittest.TestCase):
    def test_rewrite_keeps_indicator_columns(self):
        conn = fresh_db()
        b = bar()
        upsert_bar(conn, "ETH_USDT", "15m", b, indicator_cols=COLS,
                   values={"ema20": 123.0, "atr14": 4.5})
        # 同一个 tick 又来一次（未收线刷新），只更新 OHLCV
        b2 = bar(c=2401.0)
        upsert_bar(conn, "ETH_USDT", "15m", b2)
        row = conn.execute("SELECT ema20, atr14, c FROM kline").fetchone()
        self.assertEqual(row[0], 123.0)   # ← 旧实现 REPLACE 会清成 NULL
        self.assertEqual(row[1], 4.5)
        self.assertEqual(row[2], 2401.0)

    def test_upsert_no_duplicate_rows(self):
        conn = fresh_db()
        for _ in range(5):
            upsert_bar(conn, "ETH_USDT", "15m", bar())
        self.assertEqual(conn.execute("SELECT count(*) FROM kline").fetchone()[0], 1)

    def test_rejects_bad_t_and_ohlc(self):
        conn = fresh_db()
        with self.assertRaises(ValueError):
            upsert_bar(conn, "ETH_USDT", "15m", Bar(t=1_700_000_000_000, o=1, h=2, l=0.5, c=1))
        with self.assertRaises(ValueError):
            upsert_bar(conn, "ETH_USDT", "15m", Bar(t=1_700_000_000, o=10, h=9, l=8, c=10))


class TestThrottle(unittest.TestCase):
    def test_gaps(self):
        self.assertEqual(interval_seconds("15m"), 900)
        self.assertFalse(should_write_unclosed("1m", now=100.0, last_write=97.0))
        self.assertTrue(should_write_unclosed("1m", now=100.0, last_write=90.0))
        # 4h/1d 上限 300s
        self.assertFalse(should_write_unclosed("1d", now=1000.0, last_write=800.0))
        self.assertTrue(should_write_unclosed("1d", now=1000.0, last_write=600.0))
        self.assertTrue(should_write_unclosed("1m", now=100.0, last_write=None))


class TestSeriesWriter(unittest.TestCase):
    def test_closed_bars_match_core(self):
        conn = fresh_db()
        w = SeriesWriter(SPECS)
        closes = []
        for i in range(120):
            c = 2400.0 + i * 0.5
            closes.append(c)
            res = w.write(conn, "ETH_USDT", "15m", bar(t=1_700_000_000 + i * 900, c=c))
            self.assertTrue(res.written and res.closed)
        got = [r[0] for r in conn.execute(
            "SELECT ema20 FROM kline ORDER BY t")]
        self.assertEqual(got, core.ema(closes, 20))

    def test_unclosed_does_not_advance_state(self):
        conn = fresh_db()
        w = SeriesWriter(SPECS)
        # 20 根收线 → ema20 有值
        for i in range(20):
            w.write(conn, "ETH_USDT", "15m", bar(t=1_700_000_000 + i * 900, c=2400 + i))
        self.assertIsNotNone(conn.execute(
            "SELECT ema20 FROM kline ORDER BY t DESC LIMIT 1").fetchone()[0])
        # 未收线 bar 只写 OHLCV，不写指标
        res = w.write(conn, "ETH_USDT", "15m", bar(t=1_700_000_000 + 20 * 900, c=9999, closed=False),
                      now=1_800_000_000)
        self.assertTrue(res.written)
        self.assertFalse(res.closed)
        last = conn.execute("SELECT ema20, c FROM kline ORDER BY t DESC LIMIT 1").fetchone()
        self.assertIsNone(last[0])
        self.assertEqual(last[1], 9999)

    def test_throttled_unclosed_not_written(self):
        conn = fresh_db()
        w = SeriesWriter(SPECS)
        t = 1_700_000_000
        self.assertTrue(w.write(conn, "ETH_USDT", "15m", bar(t=t, closed=False), now=1000.0).written)
        self.assertFalse(w.write(conn, "ETH_USDT", "15m", bar(t=t, closed=False), now=1001.0).written)

    def test_seed_from_db_warm_start(self):
        conn = fresh_db()
        w = SeriesWriter(SPECS)
        closes = [2400.0 + i for i in range(40)]
        for i, c in enumerate(closes[:20]):
            w.write(conn, "ETH_USDT", "15m", bar(t=1_700_000_000 + i * 900, c=c))
        w2 = SeriesWriter(SPECS)
        seeded = w2.seed_from_db(conn, "ETH_USDT", "15m", warmup=20)
        self.assertEqual(seeded, 20)
        res = w2.write(conn, "ETH_USDT", "15m", bar(t=1_700_000_000 + 20 * 900, c=closes[20]))
        self.assertEqual(res.values["ema20"], core.ema(closes[:21], 20)[-1])

    def test_retention_due(self):
        w = SeriesWriter(SPECS, retention_every=3, retention_sec=10_000)
        self.assertFalse(w.due_retention(now=1.0))
        for i in range(3):
            w.write(fresh_db(), "ETH_USDT", "15m", bar(t=1_700_000_000 + i * 900, c=2400 + i))
        self.assertTrue(w.due_retention(now=2.0))


if __name__ == "__main__":
    unittest.main()
