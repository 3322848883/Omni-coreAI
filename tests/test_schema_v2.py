# -*- coding: utf-8 -*-
"""kline.db schema 双版本契约：v1（旧）/ v2（REAL + closed/src + 动态指标列）。

核心语义变化（相对旧契约）：
- **最小可用集只有 OHLCV 九列**，`ema20/atr14` 从"必需"降为"可选"（v2 由配置决定指标列）；
- `validate_kline_schema()` 返回**探测到的版本**，不再恒为 1；
- 缺 `closed` 判 v1、有 `closed`/`src` 判 v2。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contracts.kline_schema import (  # noqa: E402
    KLINE_SCHEMA_VERSION,
    REQUIRED_COLUMNS,
    SchemaError,
    detect_version,
    indicator_columns,
    schema_info,
    schema_ok,
    validate_kline_schema,
)


def _make_v1(tmp: Path) -> Path:
    db = tmp / "kline.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE kline (t INTEGER NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL,"
        " o TEXT NOT NULL, h TEXT NOT NULL, l TEXT NOT NULL, c TEXT NOT NULL,"
        " v TEXT NOT NULL, sum TEXT NOT NULL, ema20 TEXT, atr14 TEXT,"
        " PRIMARY KEY (symbol, interval, t))"
    )
    conn.execute("INSERT INTO kline VALUES (1700000000,'ETH_USDT','15m',"
                 "'1','2','0.5','1.5','10','15','1.4','0.9')")
    conn.commit()
    conn.close()
    return db


def _make_v2(tmp: Path, extra_cols=("ema20", "ema50", "ema200", "atr14", "rsi14",
                                   "macd", "macd_dea", "macd_hist",
                                   "boll20", "boll20_upper", "boll20_lower")) -> Path:
    db = tmp / "kline.db"
    conn = sqlite3.connect(db)
    cols = "".join(f", {c} REAL" for c in extra_cols)
    conn.execute(
        "CREATE TABLE kline (symbol TEXT NOT NULL, interval TEXT NOT NULL,"
        " t INTEGER NOT NULL, o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL,"
        " c REAL NOT NULL, v REAL NOT NULL, sum REAL NOT NULL,"
        " closed INTEGER NOT NULL DEFAULT 1, src TEXT NOT NULL DEFAULT 'ws'"
        f"{cols}, PRIMARY KEY (symbol, interval, t)) WITHOUT ROWID"
    )
    conn.commit()
    conn.close()
    return db


class TestVersions(unittest.TestCase):
    def test_constant_is_two(self):
        self.assertEqual(KLINE_SCHEMA_VERSION, 2)

    def test_v1_detected(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_v1(Path(td))
            self.assertEqual(validate_kline_schema(db), 1)
            self.assertTrue(schema_ok(db))

    def test_v2_detected(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_v2(Path(td))
            self.assertEqual(validate_kline_schema(db), 2)

    def test_detect_version_from_columns(self):
        self.assertEqual(detect_version(REQUIRED_COLUMNS), 1)
        self.assertEqual(detect_version(list(REQUIRED_COLUMNS) + ["closed"]), 2)


class TestMinimalRequirements(unittest.TestCase):
    def test_missing_ema20_is_ok_in_v2(self):
        """v2 起指标列可选：没有 ema20 也能用（配置里没配就不该有列）。"""
        with tempfile.TemporaryDirectory() as td:
            db = _make_v2(Path(td), extra_cols=("ema200",))
            self.assertEqual(validate_kline_schema(db), 2)

    def test_missing_ohlcv_raises(self):
        with tempfile.TemporaryDirectory() as td:
            db = tmp = Path(td) / "kline.db"
            conn = sqlite3.connect(tmp)
            cols = [c for c in REQUIRED_COLUMNS if c != "c"]
            col_sql = ", ".join(f"{c} TEXT" for c in cols)
            conn.execute(f"CREATE TABLE kline ({col_sql})")
            conn.commit()
            conn.close()
            with self.assertRaises(SchemaError):
                validate_kline_schema(db)
            self.assertFalse(schema_ok(db))

    def test_missing_file(self):
        self.assertFalse(schema_ok(None))
        self.assertFalse(schema_ok(Path("/no/such/kline.db")))

    def test_no_table_raises(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "kline.db"
            sqlite3.connect(db).close()
            with self.assertRaises(SchemaError):
                validate_kline_schema(db)


class TestIndicatorColumns(unittest.TestCase):
    def test_picks_indicator_columns(self):
        cols = list(REQUIRED_COLUMNS) + ["closed", "src", "ema20", "macd", "macd_dea",
                                         "boll20_upper", "boll20_2p5_lower", "foo"]
        self.assertEqual(indicator_columns(cols),
                         ["ema20", "macd", "macd_dea", "boll20_upper", "boll20_2p5_lower"])

    def test_schema_info_shape(self):
        with tempfile.TemporaryDirectory() as td:
            db = _make_v2(Path(td))
            info = schema_info(db)
            self.assertTrue(info["ok"])
            self.assertEqual(info["version"], 2)
            self.assertTrue(info["has_closed"] and info["has_src"])
            self.assertIn("ema200", info["indicators"])
            self.assertEqual(info["missing"], [])


if __name__ == "__main__":
    unittest.main()
