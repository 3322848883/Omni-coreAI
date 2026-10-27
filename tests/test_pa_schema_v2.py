# -*- coding: utf-8 -*-
"""写入端 v2 结构管理（pa-data-source/schema_v2.py）。"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

import schema_v2 as sv2  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402


def _v1_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE kline (t INTEGER NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL,"
        " o TEXT, h TEXT, l TEXT, c TEXT, v TEXT, sum TEXT, ema20 TEXT, atr14 TEXT,"
        " PRIMARY KEY (symbol, interval, t))"
    )
    return conn


class TestInit(unittest.TestCase):
    def test_init_creates_v2(self):
        conn = sqlite3.connect(":memory:")
        sv2.init_v2(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(kline)")]
        for c in ("symbol", "interval", "t", "o", "h", "l", "c", "v", "sum", "closed", "src"):
            self.assertIn(c, cols)
        self.assertEqual(sv2.detect_version(conn), 2)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_init_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        sv2.init_v2(conn)
        sv2.init_v2(conn)
        self.assertEqual(sv2.detect_version(conn), 2)

    def test_detect_v1(self):
        conn = _v1_db()
        self.assertEqual(sv2.detect_version(conn), 1)

    def test_detect_missing_table(self):
        self.assertIsNone(sv2.detect_version(sqlite3.connect(":memory:")))


class TestIndicatorColumns(unittest.TestCase):
    def test_adds_all_configured_columns_once(self):
        conn = sqlite3.connect(":memory:")
        sv2.init_v2(conn)
        specs = parse_indicators(None)
        added = sv2.ensure_indicator_columns(conn, specs)
        self.assertEqual(len(added), 11)
        self.assertIn("ema200", added)
        self.assertIn("boll20_upper", added)
        self.assertEqual(sv2.ensure_indicator_columns(conn, specs), [])

    def test_only_configured_columns(self):
        conn = sqlite3.connect(":memory:")
        sv2.init_v2(conn)
        added = sv2.ensure_indicator_columns(conn, parse_indicators([{"ema": [200]}]))
        self.assertEqual(added, ["ema200"])

    def test_too_many_columns_rejected(self):
        conn = sqlite3.connect(":memory:")
        sv2.init_v2(conn)
        specs = parse_indicators([{"ema": list(range(2, 70))}])
        with self.assertRaises(ValueError):
            sv2.ensure_indicator_columns(conn, specs)

    def test_requires_table(self):
        with self.assertRaises(RuntimeError):
            sv2.ensure_indicator_columns(sqlite3.connect(":memory:"), parse_indicators(None))


class TestMeta(unittest.TestCase):
    def test_roundtrip(self):
        conn = sqlite3.connect(":memory:")
        sv2.init_v2(conn)
        self.assertIsNone(sv2.get_meta(conn, "indicator_spec_hash"))
        sv2.set_meta(conn, "indicator_spec_hash", "abc123")
        self.assertEqual(sv2.get_meta(conn, "indicator_spec_hash"), "abc123")
        sv2.set_meta(conn, "indicator_spec_hash", "def456")
        self.assertEqual(sv2.get_meta(conn, "indicator_spec_hash"), "def456")


if __name__ == "__main__":
    unittest.main()
