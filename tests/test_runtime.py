import json
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.ledger import Ledger, default_ledger_path
from gate_bot.migrate import migrate_all, migrate_bot
from gate_bot.paths import BotPaths, detect_legacy_layout, list_bot_ids
from gate_bot.pidlock import PidLock


class TestPaths(unittest.TestCase):
    def test_layout_skeleton(self):
        with tempfile.TemporaryDirectory() as td:
            bp = BotPaths(Path(td), "alpha")
            bp.ensure()
            self.assertTrue(bp.inbox.is_dir())
            self.assertTrue(bp.archive_done.is_dir())
            self.assertTrue(bp.state.is_dir())
            self.assertEqual(bp.layout_version(), 2)
            self.assertIn("alpha", list_bot_ids(Path(td)))

    def test_bad_bot_id(self):
        with self.assertRaises(ValueError):
            BotPaths(Path("."), "../evil")

    def test_legacy_detect(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "inbox" / "oldbot").mkdir(parents=True)
            (root / "inbox" / "oldbot" / "s.json").write_text("{}", encoding="utf-8")
            self.assertTrue(detect_legacy_layout(root, "oldbot"))
            self.assertFalse(detect_legacy_layout(root, "newbot"))
            (root / "inbox" / "emptybot").mkdir(parents=True)
            self.assertFalse(detect_legacy_layout(root, "emptybot"))


class TestLedger(unittest.TestCase):
    def test_crud_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td) / "bots.db")
            led.insert_trade("a", plan_cycle="c1", action="open_long", symbol="BTC_USDT",
                             size_usd=10, ok=True, steps=[{"k": 1}])
            led.insert_plan("a", cycle_id="c1", trigger="interval", orders=1, reasoning="x")
            led.insert_signal("a", "inbox/a/1.json", "done")
            led.heartbeat("a", "run", pid=123, detail="alive")
            led.heartbeat("a", "run", pid=124, detail="alive")
            self.assertEqual(len(led.recent_trades("a")), 1)
            self.assertEqual(len(led.recent_plans("a")), 1)
            hb = led.heartbeats()
            self.assertEqual(len(hb), 1)
            self.assertEqual(hb[0]["pid"], 124)
            led.close()

    def test_default_path(self):
        p = default_ledger_path(Path("/x"))
        self.assertEqual(p.parts[-2:], ("data", "bots.db"))


class TestMigrate(unittest.TestCase):
    def test_migrate_moves_and_imports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "inbox" / "b1").mkdir(parents=True)
            (root / "inbox" / "b1" / "s.json").write_text("{}", encoding="utf-8")
            (root / "history" / "b1").mkdir(parents=True)
            (root / "history" / "b1" / "last_cycle.json").write_text("{}", encoding="utf-8")
            trades = root / "logs" / "trades"
            trades.mkdir(parents=True)
            (trades / "b1.jsonl").write_text(
                json.dumps({"ts": "2026-01-01T00:00:00+00:00", "type": "execution",
                            "ok": True, "steps": [], "plan_cycle": "c1"}) + "\n",
                encoding="utf-8",
            )
            rep = migrate_bot(root, "b1")
            bp = BotPaths(root, "b1")
            self.assertTrue((bp.inbox / "s.json").exists())
            self.assertTrue((bp.state / "last_cycle.json").exists())
            self.assertTrue((bp.logs / "trades.jsonl").exists())
            self.assertGreaterEqual(rep.get("imported_trades") or 0, 1)
            led = Ledger(default_ledger_path(root))
            self.assertGreaterEqual(len(led.recent_trades("b1")), 1)
            led.close()
            # idempotent
            rep2 = migrate_bot(root, "b1")
            self.assertIn("skipped", rep2)

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "inbox" / "b2").mkdir(parents=True)
            (root / "inbox" / "b2" / "x.json").write_text("{}", encoding="utf-8")
            rep = migrate_bot(root, "b2", dry_run=True)
            self.assertTrue(rep["dry_run"])
            self.assertFalse((BotPaths(root, "b2").inbox / "x.json").exists())


class TestPidLock(unittest.TestCase):
    def test_exclusive(self):
        import subprocess
        import sys as _sys

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.lock"
            dummy = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                p.write_text(str(dummy.pid), encoding="utf-8")
                self.assertIsNone(PidLock(p).acquire())
            finally:
                dummy.kill()
                dummy.wait(timeout=5)
            p.write_text("999999999", encoding="utf-8")
            l1 = PidLock(p).acquire()
            self.assertIsNotNone(l1)
            l1.release()
            self.assertIsNotNone(PidLock(p).acquire())


if __name__ == "__main__":
    unittest.main()
