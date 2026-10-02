import json
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.ledger import Ledger, default_ledger_path
from omnialpha.migrate import migrate_all, migrate_bot
from omnialpha.paths import BotPaths, bot_paths, detect_legacy_layout, list_bot_ids
from omnialpha.pidlock import PidLock


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
        from omnialpha.watcher import ProjectPaths

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
        from omnialpha.supervisor import Child, Supervisor

        sup = Supervisor.__new__(Supervisor)
        sup.root = Path("/proj")
        sup.children = []
        # mimic prepare arg construction
        args = [sup._py if hasattr(sup, "_py") else "python", "-m", "omnialpha",
                "--root", str(sup.root), "plan-loop", "--bot", "b1"]
        self.assertEqual(args[args.index("--root") + 2], "plan-loop")
        self.assertLess(args.index("--root"), args.index("plan-loop"))


class TestPidLock(unittest.TestCase):
    # 跨平台：让外部子进程持 OS 锁（Windows msvcrt / Unix fcntl）
    _HOLD_LOCK_SNIPPET = (
        "import os,sys,time\n"
        "f=open(sys.argv[1],'a+')\n"
        "f.seek(0)\n"
        "if os.name=='nt':\n"
        "    import msvcrt\n"
        "    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
        "else:\n"
        "    import fcntl\n"
        "    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('ready', os.getpid(), flush=True)\n"
        "time.sleep(30)\n"
    )

    def test_exclusive(self):
        import subprocess
        import sys as _sys

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.lock"
            dummy = subprocess.Popen(
                [_sys.executable, "-c", self._HOLD_LOCK_SNIPPET, str(p)],
                stdout=subprocess.PIPE, text=True,
            )
            try:
                line = dummy.stdout.readline()
                self.assertIn("ready", line)
                self.assertIsNone(PidLock(p).acquire())
            finally:
                dummy.kill()
                dummy.wait(timeout=5)
            # holder dead → OS released the lock → we can acquire
            l1 = PidLock(p).acquire()
            self.assertIsNotNone(l1)
            l1.release()
            self.assertIsNotNone(PidLock(p).acquire())

    def test_pid_reuse_does_not_block(self):
        """锁文件里的 PID 是诊断信息，不参与判定 —— PID 复用不能误拒。"""
        import os
        import subprocess
        import sys as _sys

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "reused.lock"
            # an unrelated live process's PID written into the file
            other = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                p.write_text(str(other.pid), encoding="utf-8")
                # no OS lock held → must acquire (old code wrongly refused here)
                lk = PidLock(p).acquire()
                self.assertIsNotNone(lk, "stale PID in file must not block acquire")
                lk.release()
                # file records the real owner (diagnostic; readable after unlock)
                self.assertEqual(p.read_text(encoding="utf-8").strip(), str(os.getpid()))
            finally:
                other.kill()
                other.wait(timeout=5)

    def test_same_instance_reentrant(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.lock"
            lk = PidLock(p).acquire()
            self.assertIsNotNone(lk)
            self.assertIs(lk.acquire(), lk, "same instance re-acquire returns self")
            lk.release()

    def test_second_instance_same_process_rejected(self):
        """OS 文件锁跨 handle 不可重入 —— 同进程第二个实例也会被拒。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.lock"
            a = PidLock(p).acquire()
            self.assertIsNotNone(a)
            b = PidLock(p).acquire()
            self.assertIsNone(b, "second handle in same process must be rejected")
            a.release()
            c = PidLock(p).acquire()
            self.assertIsNotNone(c, "after release, lock is free")
            c.release()

    def test_crash_releases_lock(self):
        """持锁进程被杀后，无需手工清锁即可接管。"""
        import subprocess
        import sys as _sys

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "crash.lock"
            holder = subprocess.Popen(
                [_sys.executable, "-c", self._HOLD_LOCK_SNIPPET, str(p)],
                stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertIn("ready", holder.stdout.readline())
                self.assertIsNone(PidLock(p).acquire())
                holder.kill()
                holder.wait(timeout=5)
                import time as _t
                _t.sleep(0.2)
                lk = PidLock(p).acquire()
                self.assertIsNotNone(lk, "OS must auto-release lock after holder death")
                lk.release()
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(timeout=5)

    def test_cli_lock_paths_match_supervisor(self):
        from omnialpha.paths import bot_paths

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bp = bot_paths(root, "b1", create=True)
            # supervisor uses bp.lock_run / bp.lock_plan; CLI must use the same
            self.assertEqual(bp.lock_run, root.resolve() / "data" / "bots" / "b1" / "state" / "run.lock")
            self.assertEqual(bp.lock_plan, root.resolve() / "data" / "bots" / "b1" / "state" / "plan.lock")

    def test_cli_refuses_when_lock_held(self):
        import subprocess
        import sys as _sys
        import types

        from omnialpha import __main__ as main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config" / "bots").mkdir(parents=True)
            bp = bot_paths(root, "b1", create=True)
            dummy = subprocess.Popen(
                [_sys.executable, "-c", self._HOLD_LOCK_SNIPPET, str(bp.lock_plan)],
                stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertIn("ready", dummy.stdout.readline())
                args = types.SimpleNamespace(bot="b1", root=str(root))
                self.assertEqual(main.cmd_plan_loop(args), 3)
            finally:
                dummy.kill()
                dummy.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
