# -*- coding: utf-8 -*-
"""account.db 运维（pa-data-source/account_maintenance.py）。"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

from account_maintenance import (ACCOUNT_TABLES, THRESHOLDS, account_db_health,  # noqa: E402
                                cleanup_account_db)

NOW = 1_800_000_000


def make_db(tables=ACCOUNT_TABLES) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for t in tables:
        conn.execute(f"CREATE TABLE [{t}] (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     " timestamp INTEGER, contract TEXT)")
    return conn


def insert(conn, table, ts_list):
    conn.executemany(f"INSERT INTO [{table}](timestamp, contract) VALUES (?, 'ETH_USDT')",
                     [(t,) for t in ts_list])
    conn.commit()


class TestCleanup(unittest.TestCase):
    def test_deletes_old_keeps_new_across_all_tables(self):
        conn = make_db()
        old = NOW - 120 * 86400
        new = NOW - 10 * 86400
        for t in ACCOUNT_TABLES:
            insert(conn, t, [old, old, new])
        res = cleanup_account_db(conn, days=90, now=NOW)
        self.assertEqual(res["total"], 2 * len(ACCOUNT_TABLES))
        for t in ACCOUNT_TABLES:
            self.assertEqual(conn.execute(f"SELECT count(*) FROM [{t}]").fetchone()[0], 1)

    def test_price_order_history_is_included(self):
        """旧实现漏掉这张表 → 只增不减。"""
        conn = make_db()
        insert(conn, "price_order_history", [NOW - 200 * 86400])
        res = cleanup_account_db(conn, days=90, now=NOW)
        self.assertEqual(res["deleted"].get("price_order_history"), 1)

    def test_dry_run_keeps_rows(self):
        conn = make_db()
        insert(conn, "position_history", [NOW - 200 * 86400])
        res = cleanup_account_db(conn, days=90, now=NOW, dry_run=True)
        self.assertEqual(res["total"], 1)
        self.assertEqual(
            conn.execute("SELECT count(*) FROM position_history").fetchone()[0], 1)

    def test_batching(self):
        conn = make_db()
        insert(conn, "position_history", [NOW - 100 * 86400] * 25)
        res = cleanup_account_db(conn, days=90, now=NOW, batch=10)
        self.assertEqual(res["deleted"]["position_history"], 25)

    def test_missing_table_skipped(self):
        conn = make_db(tables=("position_history",))
        res = cleanup_account_db(conn, days=90, now=NOW)
        self.assertEqual(set(res["deleted"]), {"position_history"})


class TestHealth(unittest.TestCase):
    def test_shape_and_no_warnings(self):
        conn = make_db()
        insert(conn, "position_history", [NOW - 5 * 86400])
        h = account_db_health(conn, size_mb=84.0, disk_free_gb=50.0, now=NOW)
        self.assertEqual(h["rows"], 1)
        self.assertAlmostEqual(h["oldest_days"], 5.0, places=0)
        self.assertEqual(h["warnings"], [])
        self.assertIn("position_history", h["per_table"])

    def test_warnings(self):
        conn = make_db()
        insert(conn, "position_history", [NOW - 200 * 86400])
        h = account_db_health(conn, size_mb=600.0, disk_free_gb=1.0, now=NOW)
        self.assertEqual(len(h["warnings"]), 3)  # size / oldest / disk
        self.assertTrue(any("account_db_size" in w for w in h["warnings"]))
        self.assertLess(THRESHOLDS["disk_free_gb"], 10)


if __name__ == "__main__":
    unittest.main()
