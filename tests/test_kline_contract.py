"""Contract tests: kline.db schema between pa-data-source and gate_bot (read-only)."""
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
    schema_ok,
    validate_kline_schema,
)
from gate_bot.strategist.market import MarketConfig, resolve_db_path, resolve_pa_data_root  # noqa: E402


class TestKlineContract(unittest.TestCase):
    def _make_db(self, tmp: Path, cols=REQUIRED_COLUMNS):
        db = tmp / "kline.db"
        conn = sqlite3.connect(db)
        col_sql = ", ".join(f"{c} TEXT" if c != "t" else "t INTEGER" for c in cols)
        # symbol/interval need to be queryable; t INTEGER, rest TEXT is fine for contract
        conn.execute(f"CREATE TABLE kline ({col_sql})")
        conn.commit()
        conn.close()
        return db

    def test_schema_version_constant(self):
        self.assertEqual(KLINE_SCHEMA_VERSION, 1)

    def test_validate_ok(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._make_db(Path(td))
            self.assertEqual(validate_kline_schema(db), 1)
            self.assertTrue(schema_ok(db))

    def test_validate_missing_column(self):
        with tempfile.TemporaryDirectory() as td:
            cols = [c for c in REQUIRED_COLUMNS if c != "ema20"]
            db = self._make_db(Path(td), cols=cols)
            with self.assertRaises(SchemaError):
                validate_kline_schema(db)
            self.assertFalse(schema_ok(db))

    def test_validate_missing_file(self):
        self.assertFalse(schema_ok(None))
        self.assertFalse(schema_ok(Path("/no/such/kline.db")))

    def test_monorepo_default_path(self):
        cfg = MarketConfig(mode="hybrid")
        root = resolve_pa_data_root(cfg, bot_root=Path("/proj"))
        self.assertEqual(root, Path("/proj/pa-data-source/data").resolve())
        live = resolve_db_path("live", cfg, bot_root=Path("/proj"))
        testnet = resolve_db_path("testnet", cfg, bot_root=Path("/proj"))
        self.assertTrue(str(live).endswith("kline.db"))
        self.assertTrue(str(testnet).endswith("kline_testnet.db"))

    def test_pa_source_present_in_monorepo(self):
        pa = ROOT / "pa-data-source"
        self.assertTrue((pa / "kline_watcher.py").exists())
        self.assertTrue((pa / "watchdog.py").exists())
        self.assertTrue((pa / "watchlist.yaml").exists())
        # no committed runtime dbs
        dbs = list(pa.glob("data/*.db")) + list(pa.glob("*.db"))
        self.assertEqual(dbs, [])


if __name__ == "__main__":
    unittest.main()
