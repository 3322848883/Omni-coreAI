# -*- coding: utf-8 -*-
"""写入引擎选择层（pa-data-source/ingest.py）：legacy / shadow / v2。

三条契约
1. **legacy 与旧实现逐位一致**：EMA k=2/21 单步递推、ATR `(prev*13+tr)/14`、round 8 位；
2. **shadow 写旧值、只比对**：下游零影响，用 ShadowStats 累积失配率（门禁 0.1%）；
3. **v2 用 StreamState 增量**，且**列感知**：v1 表（无 closed/src、只有两列）也能跑。
"""
import os
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
from ingest import SeriesRegistry, legacy_values, resolve_engine  # noqa: E402
from writer_v2 import Bar  # noqa: E402

SPECS = parse_indicators([{"ema": [20]}, {"atr": [14]}])


def v1_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE kline (t INTEGER NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL,"
        " o TEXT, h TEXT, l TEXT, c TEXT, v TEXT, sum TEXT, ema20 TEXT, atr14 TEXT,"
        " PRIMARY KEY (symbol, interval, t))")
    return conn


def v2_db():
    conn = sqlite3.connect(":memory:")
    sv2.init_v2(conn)
    sv2.ensure_indicator_columns(conn, SPECS)
    return conn


def bar(i, c=None, closed=True):
    c = 2400.0 + i if c is None else c
    return Bar(t=1_700_000_000 + i * 900, o=c, h=c + 1, l=c - 1, c=c, v=10.0,
               closed=closed, src="ws")


class TestResolveEngine(unittest.TestCase):
    def test_default_legacy(self):
        for k in ("PA_INDICATOR_ENGINE", "PA_KLINE_V2"):
            os.environ.pop(k, None)
        self.assertEqual(resolve_engine(), "legacy")

    def test_env_v2(self):
        os.environ["PA_KLINE_V2"] = "1"
        try:
            self.assertEqual(resolve_engine(), "v2")
        finally:
            os.environ.pop("PA_KLINE_V2", None)

    def test_explicit_wins(self):
        os.environ["PA_INDICATOR_ENGINE"] = "v2"
        try:
            self.assertEqual(resolve_engine("shadow"), "shadow")
            with self.assertRaises(ValueError):
                resolve_engine("nope")
        finally:
            os.environ.pop("PA_INDICATOR_ENGINE", None)


class TestLegacyValues(unittest.TestCase):
    def test_matches_old_formula(self):
        conn = v1_db()
        b0 = bar(0, c=2400.0)
        conn.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (b0.t, "ETH_USDT", "15m", "2400", "2401", "2399", "2400", "10", "0",
                      "2398.5", "5.0"))
        b1 = bar(1, c=2410.0)
        vals = legacy_values(conn, "ETH_USDT", "15m", b1)
        k = 2.0 / 21.0
        self.assertAlmostEqual(vals["ema20"], round(2410 * k + 2398.5 * (1 - k), 8), places=9)
        tr = max(2411 - 2409, abs(2411 - 2400), abs(2409 - 2400))
        self.assertAlmostEqual(vals["atr14"], round((5.0 * 13 + tr) / 14, 8), places=9)

    def test_no_prev_row_returns_none(self):
        conn = v1_db()
        vals = legacy_values(conn, "ETH_USDT", "15m", bar(0))
        self.assertIsNone(vals["ema20"])
        self.assertIsNone(vals["atr14"])


class TestLegacyEngine(unittest.TestCase):
    def test_writes_v1_table(self):
        conn = v1_db()
        reg = SeriesRegistry(SPECS, engine="legacy")
        conn.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (bar(0).t, "ETH_USDT", "15m", "2400", "2401", "2399", "2400",
                      "10", "0", "2398.5", "5.0"))
        out = reg.write(conn, "ETH_USDT", "15m", bar(1, c=2410.0))
        self.assertEqual(out["engine"], "legacy")
        row = conn.execute("SELECT ema20, atr14 FROM kline ORDER BY t DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNotNone(row[1])


def seed_history(conn, n=40):
    """按旧代码的方式播种：写入 40 根并把指标算好（等价 watcher 的 recalculate_all_ema）。

    **legacy 不能自举**：它只从「上一根 ema20 非 NULL」递推，新库必须靠一次全量重算播种。
    """
    closes = [bar(i).c for i in range(n)]
    h = [bar(i).h for i in range(n)]
    l = [bar(i).l for i in range(n)]
    ema = core.ema(closes, 20)
    atr = core.atr(h, l, closes, 14)
    for i in range(n):
        b = bar(i)
        conn.execute(
            "INSERT INTO kline(symbol,interval,t,o,h,l,c,v,sum,closed,src,ema20,atr14)"
            " VALUES (?,?,?,?,?,?,?,?,?,1,'ws',?,?)",
            ("ETH_USDT", "15m", b.t, b.o, b.h, b.l, b.c, b.v, 0.0,
             None if ema[i] is None else round(ema[i], 8),
             None if atr[i] is None else round(atr[i], 8)))
    conn.commit()


class TestShadowEngine(unittest.TestCase):
    def test_writes_legacy_values_and_records_stats(self):
        conn = v2_db()
        seed_history(conn, 40)
        reg = SeriesRegistry(SPECS, engine="shadow")
        # 让新引擎状态与库对齐（等价重启后 prime）
        reg.prime(conn, "ETH_USDT", "15m")
        out = reg.write(conn, "ETH_USDT", "15m", bar(40))
        summary = reg.shadow_summary()
        self.assertGreater(summary["n"], 0)
        self.assertIsNotNone(out["values"]["ema20"])
        # 写进库的必须是 legacy 值（下游零影响）
        stored = conn.execute("SELECT ema20 FROM kline ORDER BY t DESC LIMIT 1").fetchone()[0]
        self.assertAlmostEqual(stored, out["values"]["ema20"], places=9)
        # 预热对齐后两套引擎应基本一致
        self.assertLess(summary["ratio"], 0.5, summary)

    def test_no_prev_means_no_stats(self):
        conn = v2_db()
        reg = SeriesRegistry(SPECS, engine="shadow")
        for i in range(5):  # 库里没有可递推的前值 → 双方都 None
            reg.write(conn, "ETH_USDT", "15m", bar(i))
        self.assertEqual(reg.shadow_summary()["n"], 0)


class TestV2Engine(unittest.TestCase):
    def test_writes_configured_columns_only(self):
        conn = v2_db()
        reg = SeriesRegistry(SPECS, engine="v2")
        for i in range(30):
            reg.write(conn, "ETH_USDT", "15m", bar(i))
        closes = [bar(i).c for i in range(30)]
        got = [r[0] for r in conn.execute("SELECT ema20 FROM kline ORDER BY t")]
        self.assertEqual(got, core.ema(closes, 20))
        # 未配置的列根本没被创建（配置驱动：配了才算、才算落库）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kline)").fetchall()}
        self.assertIn("ema20", cols)
        self.assertNotIn("ema200", cols)

    def test_unclosed_throttled_and_no_indicator(self):
        conn = v2_db()
        reg = SeriesRegistry(SPECS, engine="v2")
        for i in range(20):
            reg.write(conn, "ETH_USDT", "15m", bar(i))
        b = bar(20, closed=False)
        first = reg.write(conn, "ETH_USDT", "15m", b, now=1000.0)
        second = reg.write(conn, "ETH_USDT", "15m", b, now=1001.0)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        row = conn.execute(
            "SELECT ema20, closed FROM kline ORDER BY t DESC LIMIT 1").fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], 0)

    def test_v1_table_compatible(self):
        """v2 引擎写 v1 表：没有 closed/src 列也不能报错（列感知）。"""
        conn = v1_db()
        reg = SeriesRegistry(SPECS, engine="v2")
        for i in range(25):
            reg.write(conn, "ETH_USDT", "15m", bar(i))
        self.assertEqual(conn.execute("SELECT count(*) FROM kline").fetchone()[0], 25)
        self.assertIsNotNone(
            conn.execute("SELECT ema20 FROM kline ORDER BY t DESC LIMIT 1").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
