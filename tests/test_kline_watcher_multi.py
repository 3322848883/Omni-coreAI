import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pa-data-source"))
sys.path.insert(0, str(ROOT))

import kline_watcher_multi as kwm  # noqa: E402
from contracts.kline_schema import validate_kline_schema  # noqa: E402


def _bars(n=40, t0=1_700_000_000):
    out = []
    px = 100.0
    for i in range(n):
        px += 0.4 if i % 2 == 0 else -0.2
        out.append({
            "t": t0 + i * 900,
            "o": px, "h": px + 1.0, "l": px - 1.0, "c": px,
            "v": 10 + i, "sum": 1000 + i * 10,
        })
    return out


class TestMultiWatcherDb(unittest.TestCase):
    def test_db_schema_matches_contract(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "kline_binance.db")
            conn = kwm.open_db(path)
            conn.close()
            self.assertEqual(validate_kline_schema(Path(path)), 1)

    def test_db_path_per_exchange_and_env(self):
        self.assertTrue(kwm.db_path_for("binance", "live").endswith("kline_binance.db"))
        self.assertTrue(kwm.db_path_for("binance", "testnet").endswith("kline_binance_testnet.db"))
        self.assertTrue(kwm.db_path_for("okx", "live").endswith("kline_okx.db"))
        self.assertTrue(kwm.db_path_for("hyperliquid", "testnet").endswith("kline_hyperliquid_testnet.db"))

    def test_merge_writes_and_computes_indicators(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "kline_okx.db")
            conn = kwm.open_db(path)
            rows = _bars(50)
            n = kwm.merge_and_write(conn, "BTC_USDT", "15m", rows)
            self.assertGreaterEqual(n, 50)
            got = kwm.load_sorted(conn, "BTC_USDT", "15m")
            conn.close()
            self.assertEqual(len(got), 50)
            # 末根应有 ema20 / atr14
            self.assertIsNotNone(got[-1]["ema20"])
            self.assertIsNotNone(got[-1]["atr14"])
            # 前 19 根 ema20 为 None（period=20 预热）
            self.assertIsNone(got[0]["ema20"])
            # 列名与 Gate 契约一致
            self.assertEqual(
                set(got[-1].keys()),
                {"t", "o", "h", "l", "c", "v", "sum", "ema20", "atr14"},
            )

    def test_merge_upsert_replaces_same_t(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "kline_bybit.db")
            conn = kwm.open_db(path)
            rows = _bars(30)
            kwm.merge_and_write(conn, "ETH_USDT", "15m", rows)
            # 同一 t 改 close，应替换而非插入
            upd = [dict(rows[-1], c=999.0)]
            kwm.merge_and_write(conn, "ETH_USDT", "15m", upd)
            got = kwm.load_sorted(conn, "ETH_USDT", "15m")
            conn.close()
            self.assertEqual(len(got), 30)
            self.assertEqual(float(got[-1]["c"]), 999.0)

    def test_truncate_keeps_latest(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "kline_bitget.db")
            conn = kwm.open_db(path)
            kwm.merge_and_write(conn, "BTC_USDT", "1h", _bars(60))
            kwm.truncate(conn, "BTC_USDT", "1h", max_candles=20)
            got = kwm.load_sorted(conn, "BTC_USDT", "1h")
            conn.close()
            self.assertEqual(len(got), 20)
            # 保留的是最新的
            self.assertEqual(got[-1]["t"], _bars(60)[-1]["t"])


class TestNoGateCollision(unittest.TestCase):
    def test_multi_skips_gate(self):
        # gate 由 kline_watcher 负责；多所采集不应写 kline.db
        self.assertNotIn("gate", kwm.DEFAULT_EXCHANGES)
        self.assertNotIn("kline.db", kwm.db_path_for("binance", "live"))


if __name__ == "__main__":
    unittest.main()
