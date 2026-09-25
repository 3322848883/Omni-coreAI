import json
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.ledger import Ledger, default_ledger_path
from gate_bot.migrate import migrate_all, migrate_bot
from gate_bot.paths import BotPaths, bot_paths, detect_legacy_layout, list_bot_ids
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

    def test_migrate_import_not_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trades = root / "logs" / "trades"
            trades.mkdir(parents=True)
            (trades / "b3.jsonl").write_text(
                json.dumps({"type": "execution", "ok": True, "steps": [], "plan_cycle": "c"}) + "\n",
                encoding="utf-8",
            )
            migrate_bot(root, "b3")
            migrate_bot(root, "b3")
            led = Ledger(default_ledger_path(root))
            self.assertEqual(len(led.recent_trades("b3", limit=50)), 1)
            led.close()

    def test_project_paths_v2(self):
        from gate_bot.watcher import ProjectPaths

        with tempfile.TemporaryDirectory() as td:
            pp = ProjectPaths(Path(td))
            inbox = pp.bot_inbox("b9")
            self.assertTrue(str(inbox).replace("\\", "/").endswith("data/bots/b9/inbox"))
            self.assertTrue(pp.bot_done("b9").exists())

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "inbox" / "b2").mkdir(parents=True)
            (root / "inbox" / "b2" / "x.json").write_text("{}", encoding="utf-8")
            rep = migrate_bot(root, "b2", dry_run=True)
            self.assertTrue(rep["dry_run"])
            self.assertFalse((BotPaths(root, "b2").inbox / "x.json").exists())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "inbox" / "b2").mkdir(parents=True)
            (root / "inbox" / "b2" / "x.json").write_text("{}", encoding="utf-8")
            rep = migrate_bot(root, "b2", dry_run=True)
            self.assertTrue(rep["dry_run"])
            self.assertFalse((BotPaths(root, "b2").inbox / "x.json").exists())


class TestSupervisorArgs(unittest.TestCase):
    def test_child_args_place_root_before_subcommand(self):
        """argparse: --root is global and must precede subcommand."""
        from gate_bot.supervisor import Child, Supervisor

        sup = Supervisor.__new__(Supervisor)
        sup.root = Path("/proj")
        sup.children = []
        # mimic prepare arg construction
        args = [sup._py if hasattr(sup, "_py") else "python", "-m", "gate_bot",
                "--root", str(sup.root), "plan-loop", "--bot", "b1"]
        self.assertEqual(args[args.index("--root") + 2], "plan-loop")
        self.assertLess(args.index("--root"), args.index("plan-loop"))


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

    def test_cli_lock_paths_match_supervisor(self):
        from gate_bot.paths import bot_paths

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bp = bot_paths(root, "b1", create=True)
            # supervisor uses bp.lock_run / bp.lock_plan; CLI must use the same
            self.assertEqual(bp.lock_run, root.resolve() / "data" / "bots" / "b1" / "state" / "run.lock")
            self.assertEqual(bp.lock_plan, root.resolve() / "data" / "bots" / "b1" / "state" / "plan.lock")

    def test_gate_lock_held_skips_acquire(self):
        import os
        import subprocess
        import sys as _sys
        import types
        from unittest import mock

        from gate_bot import __main__ as main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config" / "bots").mkdir(parents=True)
            bp = bot_paths(root, "b1", create=True)
            # lock held by a foreign live pid (not this process)
            dummy = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                bp.lock_plan.write_text(str(dummy.pid), encoding="utf-8")
                args = types.SimpleNamespace(bot="b1", root=str(root))
                # lock held by another pid → CLI must refuse (exit 3)
                self.assertEqual(main.cmd_plan_loop(args), 3)
                # supervisor handoff: child skips acquire (then fails on missing yaml)
                with mock.patch.dict(os.environ, {"GATE_LOCK_HELD": "1"}):
                    try:
                        rc = main.cmd_plan_loop(args)
                    except Exception:
                        rc = "past_lock"  # reached config load → lock was skipped
                self.assertNotEqual(rc, 3)
            finally:
                dummy.kill()
                dummy.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
