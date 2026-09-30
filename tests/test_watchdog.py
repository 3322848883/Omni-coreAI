# -*- coding: utf-8 -*-
"""看门狗测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate_bot.watchdog import COMPONENT_CMD, Target, Watchdog

YAML = """
bot_id: {bid}
enabled: true
env: {env}
symbols: [BTC_USDT]
max_notional_usd: 100
"""


def _make_root(td: str, bots: dict) -> Path:
    root = Path(td)
    (root / "config" / "bots").mkdir(parents=True, exist_ok=True)
    for bid, env in bots.items():
        (root / "config" / "bots" / f"{bid}.yaml").write_text(
            YAML.format(bid=bid, env=env), encoding="utf-8"
        )
    return root


class TestDiscover(unittest.TestCase):
    def test_paper_gets_plan_and_paper_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper", "l1": "live"})
            wd = Watchdog(root)
            targets = wd.discover()
            pairs = {(t.bot_id, t.component) for t in targets}
            self.assertIn(("p1", "plan"), pairs)
            self.assertIn(("p1", "paper"), pairs)   # paper → paper-run
            self.assertIn(("l1", "plan"), pairs)
            self.assertIn(("l1", "run"), pairs)     # live → run
            self.assertNotIn(("p1", "run"), pairs)
            self.assertNotIn(("l1", "paper"), pairs)

    def test_disabled_bot_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper"})
            y = root / "config" / "bots" / "p1.yaml"
            y.write_text(y.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"),
                         encoding="utf-8")
            wd = Watchdog(root)
            self.assertEqual(wd.discover(), [])

    def test_bot_ids_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper", "p2": "paper"})
            wd = Watchdog(root, bot_ids=["p1"])
            targets = wd.discover()
            self.assertTrue(all(t.bot_id == "p1" for t in targets))

    def test_component_cmd_map(self):
        self.assertEqual(COMPONENT_CMD["plan"], "plan-loop")
        self.assertEqual(COMPONENT_CMD["paper"], "paper-run")
        self.assertEqual(COMPONENT_CMD["run"], "run")


class TestAliveProbe(unittest.TestCase):
    def test_missing_lock_means_dead(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertFalse(Watchdog._alive("b1", "plan", root))

    def test_held_lock_means_alive(self):
        from gate_bot.pidlock import PidLock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_path = root / "data" / "bots" / "b1" / "state" / "plan.lock"
            lock_path.parent.mkdir(parents=True)
            holder = PidLock(lock_path)
            self.assertIsNotNone(holder.acquire())
            try:
                self.assertTrue(Watchdog._alive("b1", "plan", root))
            finally:
                holder.release()

    def test_released_lock_means_dead(self):
        from gate_bot.pidlock import PidLock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_path = root / "data" / "bots" / "b1" / "state" / "run.lock"
            lock_path.parent.mkdir(parents=True)
            holder = PidLock(lock_path)
            holder.acquire()
            holder.release()
            self.assertFalse(Watchdog._alive("b1", "paper", root))


class TestRestartLimit(unittest.TestCase):
    def test_storm_holds(self):
        wd = Watchdog(Path("."), max_restarts_per_hour=2)
        t = Target("b1", "plan")
        self.assertTrue(wd._allow_restart(t))
        self.assertTrue(wd._allow_restart(t))
        # 第 3 次 → 停手
        self.assertFalse(wd._allow_restart(t))
        self.assertTrue(t.stopped)

    def test_window_slides(self):
        wd = Watchdog(Path("."), max_restarts_per_hour=2)
        t = Target("b1", "plan")
        wd._allow_restart(t)
        wd._allow_restart(t)
        # 老记录过期
        t.restarts = [0.0, 0.0]
        self.assertTrue(wd._allow_restart(t))
        self.assertFalse(t.stopped)


class TestCheckOnce(unittest.TestCase):
    def test_spawns_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper"})
            wd = Watchdog(root, notify=False)
            wd.discover()
            spawned = []
            with mock.patch.object(wd, "_spawn", side_effect=lambda t: spawned.append(t) or True):
                with mock.patch.object(Watchdog, "_alive", return_value=False):
                    result = wd.check_once()
            self.assertEqual(len(result["restarted"]), 2)  # plan + paper
            self.assertEqual(len(spawned), 2)

    def test_skips_alive(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper"})
            wd = Watchdog(root, notify=False)
            wd.discover()
            with mock.patch.object(wd, "_spawn") as sp:
                with mock.patch.object(Watchdog, "_alive", return_value=True):
                    result = wd.check_once()
            sp.assert_not_called()
            self.assertEqual(result["restarted"], [])
            self.assertEqual(len(result["held"]), 2)

    def test_notifies_on_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper"})
            wd = Watchdog(root, notify=True)
            wd.discover()
            sent = []
            fake = mock.MagicMock()
            fake.has_channel = True
            fake.send = lambda text: sent.append(text) or True
            wd._notifier = fake
            with mock.patch.object(wd, "_spawn", return_value=True):
                with mock.patch.object(Watchdog, "_alive", return_value=False):
                    wd.check_once()
            self.assertTrue(any("自动拉起" in s for s in sent))


if __name__ == "__main__":
    unittest.main()
