# -*- coding: utf-8 -*-
"""分周期保留策略（pa-data-source/retention_policy.py）。"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

import schema_v2 as sv2  # noqa: E402
from retention_policy import (DEFAULT_LIMITS, Limits, apply_retention, parse_limits,  # noqa: E402
                              truncate, validate)


def db_with(rows, interval="1d", symbol="ETH_USDT"):
    conn = sqlite3.connect(":memory:")
    sv2.init_v2(conn)
    conn.executemany(
        "INSERT INTO kline(symbol,interval,t,o,h,l,c,v,sum,closed,src) VALUES (?,?,?,?,?,?,?,?,?,1,'ws')",
        [(symbol, interval, t, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0) for t in rows])
    conn.commit()
    return conn


class TestLimitsConfig(unittest.TestCase):
    def test_defaults(self):
        lim = parse_limits(None)
        self.assertEqual(lim["1m"].max_candles, 5_760)
        self.assertEqual(lim["1d"].max_candles, 1_600)

    def test_override_and_int_shorthand(self):
        lim = parse_limits({"15m": 4000, "1d": {"max_candles": 1800, "days": 1800}})
        self.assertEqual(lim["15m"].max_candles, 4000)
        self.assertEqual(lim["1d"].days, 1800)

    def test_validate_rejects_too_small_cap(self):
        with self.assertRaises(ValueError):
            validate({"1d": Limits(500, 1600, 1500)}, warmup=1005)

    def test_validate_rejects_days_too_short(self):
        # 1d 只留 100 天却要 1600 根 → 天数会先把数据删光
        with self.assertRaises(ValueError):
            validate({"1d": Limits(1600, 100, 1500)}, warmup=1005)

    def test_default_limits_pass_validation(self):
        validate(DEFAULT_LIMITS, warmup=1005)  # 不抛即通过


class TestTruncate(unittest.TestCase):
    def test_keeps_latest(self):
        rows = [1_700_000_000 + i * 86400 for i in range(100)]
        conn = db_with(rows)
        deleted = truncate(conn, "ETH_USDT", "1d", 30)
        self.assertEqual(deleted, 70)
        left = [r[0] for r in conn.execute("SELECT t FROM kline ORDER BY t")]
        self.assertEqual(left, rows[-30:])

    def test_noop_when_under_cap(self):
        conn = db_with([1_700_000_000])
        self.assertEqual(truncate(conn, "ETH_USDT", "1d", 30), 0)

    def test_dry_run_counts_only(self):
        rows = [1_700_000_000 + i * 86400 for i in range(50)]
        conn = db_with(rows)
        self.assertEqual(truncate(conn, "ETH_USDT", "1d", 20, dry_run=True), 30)
        self.assertEqual(conn.execute("SELECT count(*) FROM kline").fetchone()[0], 50)


class TestApplyRetention(unittest.TestCase):
    def test_time_and_rows(self):
        now = 1_800_000_000
        old = now - 400 * 86400
        rows = [old + i * 86400 for i in range(400)]
        conn = db_with(rows)
        res = apply_retention(conn, "ETH_USDT", "1d", Limits(1600, 300, 1500), now=now)
        self.assertGreater(res["deleted_time"], 0)
        left = conn.execute("SELECT count(*) FROM kline").fetchone()[0]
        self.assertEqual(left, 300)

    def test_guard_keeps_recent(self):
        now = 1_800_000_000
        conn = db_with([now - 10, now - 5, now])
        # days=0 → 不按时间删；只有行数约束
        res = apply_retention(conn, "ETH_USDT", "1d", Limits(10, 0, 1000), now=now)
        self.assertEqual(res["deleted_time"], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM kline").fetchone()[0], 3)

    def test_dry_run(self):
        now = 1_800_000_000
        conn = db_with([now - 500 * 86400, now])
        res = apply_retention(conn, "ETH_USDT", "1d", Limits(1600, 30, 1500),
                              now=now, dry_run=True)
        self.assertEqual(res["deleted_time"], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM kline").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
