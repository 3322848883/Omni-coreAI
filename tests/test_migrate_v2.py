# -*- coding: utf-8 -*-
"""v1 → v2 迁移（pa-data-source/migrate_v2.py）：门禁 + 原子切换 + 回滚。

用临时文件库（VACUUM INTO 需要文件），不碰任何真实库。
"""
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

import schema_v2 as sv2  # noqa: E402
from indicator_config import parse_indicators  # noqa: E402
from migrate_v2 import backup_db, migrate, run_gates, swap_back  # noqa: E402

SPECS = parse_indicators([{"ema": [20]}, {"atr": [14]}])


def build_v1(path: Path, n=1200, symbol="ETH_USDT", interval="15m"):
    """造一个真实形态的 v1 库（TEXT 值 + ema20/atr14 留空）。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE kline (t INTEGER NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL,"
        " o TEXT NOT NULL, h TEXT NOT NULL, l TEXT NOT NULL, c TEXT NOT NULL,"
        " v TEXT NOT NULL, sum TEXT NOT NULL, ema20 TEXT, atr14 TEXT,"
        " PRIMARY KEY (symbol, interval, t))")
    conn.execute("CREATE INDEX idx_kline_time ON kline(t)")
    rnd = random.Random(4)
    price = 2400.0
    rows = []
    for i in range(n):
        o = price
        c = max(1.0, o + rnd.gauss(0, 1.5))
        h = max(o, c) + abs(rnd.gauss(0, 0.8))
        l = min(o, c) - abs(rnd.gauss(0, 0.8))
        rows.append((1_700_000_000 + i * 900, symbol, interval,
                     str(o), str(h), str(l), str(c), "10", "0"))
        price = c
    conn.executemany("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)", rows)
    conn.commit()
    conn.close()


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db = Path(self.td.name) / "kline.db"
        build_v1(self.db)
        self.backup = Path(self.td.name) / "kline_v1_backup.db"

    def tearDown(self):
        self.td.cleanup()

    def test_full_migration(self):
        res = migrate(self.db, SPECS, backup_path=self.backup)
        self.assertTrue(res["swapped"], res.get("gates"))
        self.assertTrue(res["gates"]["all_ok"]["ok"], res["gates"])
        self.assertTrue(self.backup.exists())

        conn = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(sv2.detect_version(conn), 2)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM kline").fetchone()[0], 1200)
            # 指标已回填（预热后非空）
            last = conn.execute(
                "SELECT ema20, atr14 FROM kline ORDER BY t DESC LIMIT 1").fetchone()
            self.assertIsNotNone(last[0])
            self.assertIsNotNone(last[1])
            # 旧表留存
            self.assertEqual(
                conn.execute("SELECT count(*) FROM kline_v1_archive").fetchone()[0], 1200)
            # meta 写入
            self.assertEqual(sv2.get_meta(conn, "schema_version"), "2")
        finally:
            conn.close()

    def test_dry_run_keeps_v1(self):
        res = migrate(self.db, SPECS, backup_path=self.backup, dry_run=True)
        self.assertFalse(res["swapped"])
        conn = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(sv2.detect_version(conn), 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM kline").fetchone()[0], 1200)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("kline_v2_new", tables)
        finally:
            conn.close()

    def test_second_run_skips(self):
        migrate(self.db, SPECS, backup_path=self.backup)
        again = migrate(self.db, SPECS, backup_path=self.backup)
        self.assertEqual(again.get("skipped"), "already_v2")

    def test_rollback_after_swap(self):
        migrate(self.db, SPECS, backup_path=self.backup)
        conn = sqlite3.connect(str(self.db))
        try:
            swap_back(conn)
            self.assertEqual(sv2.detect_version(conn), 1)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM kline").fetchone()[0], 1200)
        finally:
            conn.close()

    def test_gates_detect_row_loss(self):
        conn = sqlite3.connect(str(self.db))
        try:
            from migrate_v2 import copy_rows, create_targets
            create_targets(conn)
            copy_rows(conn, SPECS)
            # WITHOUT ROWID 表没有 rowid，按 (symbol,interval,t) 删
            conn.execute("DELETE FROM kline_v2_new WHERE (symbol, interval, t) IN "
                         "(SELECT symbol, interval, t FROM kline_v2_new ORDER BY t LIMIT 5)")
            conn.commit()
            gates = run_gates(conn, SPECS, warmup=max(s.warmup for s in SPECS))
            self.assertFalse(gates["row_count"]["ok"])
            self.assertFalse(gates["all_ok"]["ok"])
        finally:
            conn.close()

    def test_backup_reports_rows(self):
        info = backup_db(self.db, self.backup)
        self.assertEqual(info["rows"], 1200)
        self.assertGreater(info["size_mb"], 0)


if __name__ == "__main__":
    unittest.main()
