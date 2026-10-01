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
enabled: false
env: {env}
symbols: [BTC_USDT]
max_notional_usd: 100
"""


def _make_root(td: str, bots: dict) -> Path:
    """造 root：基线（enabled: false）+ overlay（enabled: true）。

    注意：基线 enabled 被引擎忽略，启用必须走 config/bots.local/。
    """
    root = Path(td)
    (root / "config" / "bots").mkdir(parents=True, exist_ok=True)
    (root / "config" / "bots.local").mkdir(parents=True, exist_ok=True)
    for bid, env in bots.items():
        (root / "config" / "bots" / f"{bid}.yaml").write_text(
            YAML.format(bid=bid, env=env), encoding="utf-8"
        )
        (root / "config" / "bots.local" / f"{bid}.yaml").write_text(
            "enabled: true\n", encoding="utf-8"
        )
    return root


def _touch_locks(root: Path, *bot_ids: str) -> None:
    for bid in bot_ids:
        st = root / "data" / "bots" / bid / "state"
        st.mkdir(parents=True, exist_ok=True)
        (st / "plan.lock").write_text("0", encoding="utf-8")
        (st / "run.lock").write_text("0", encoding="utf-8")


class TestDiscover(unittest.TestCase):
    def test_paper_gets_plan_and_paper_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper", "l1": "live"})
            _touch_locks(root, "p1", "l1")
            wd = Watchdog(root)
            pairs = {(t.bot_id, t.component) for t in wd.discover()}
            self.assertIn(("p1", "plan"), pairs)
            self.assertIn(("p1", "paper"), pairs)   # paper → paper-run
            self.assertIn(("l1", "plan"), pairs)
            self.assertIn(("l1", "run"), pairs)     # live → run
            self.assertNotIn(("p1", "run"), pairs)
            self.assertNotIn(("l1", "paper"), pairs)

    def test_disabled_bot_skipped(self):
        """enabled 是唯一开关：无 overlay（或 overlay=false）一律不看。"""
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"on1": "paper", "off1": "paper"})
            # 禁用 = 移除 overlay（基线本身不可启用）
            (root / "config" / "bots.local" / "off1.yaml").unlink()
            _touch_locks(root, "on1", "off1")
            wd = Watchdog(root)
            bids = {t.bot_id for t in wd.discover()}
            self.assertEqual(bids, {"on1"})

    def test_bot_ids_filter_narrows(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper", "p2": "paper"})
            _touch_locks(root, "p1", "p2")
            wd = Watchdog(root, bot_ids=["p1"])
            bids = {t.bot_id for t in wd.discover()}
            self.assertEqual(bids, {"p1"})

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
        wd = Watchdog(Path("."), max_restarts_per_hour=2, notify=False)
        t = Target("b1", "plan")
        self.assertTrue(wd._allow_restart(t))
        self.assertTrue(wd._allow_restart(t))
        self.assertFalse(wd._allow_restart(t))
        self.assertTrue(t.stopped)

    def test_window_slides(self):
        wd = Watchdog(Path("."), max_restarts_per_hour=2, notify=False)
        t = Target("b1", "plan")
        wd._allow_restart(t)
        wd._allow_restart(t)
        t.restarts = [0.0, 0.0]
        self.assertTrue(wd._allow_restart(t))
        self.assertFalse(t.stopped)


class TestCheckOnce(unittest.TestCase):
    def test_spawns_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper"})
            _touch_locks(root, "p1")
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
            _touch_locks(root, "p1")
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
            _touch_locks(root, "p1")
            wd = Watchdog(root, notify=True)
            wd.discover()
            sent = []
            with mock.patch(
                "gate_bot.monitoring.notify_process_event",
                side_effect=lambda **kw: sent.append(kw.get("title", "")) or True,
            ):
                with mock.patch.object(wd, "_spawn", return_value=True):
                    with mock.patch.object(Watchdog, "_alive", return_value=False):
                        wd.check_once()
            self.assertTrue(any("自动拉起" in s for s in sent), f"sent={sent}")

    def test_no_notify_sends_nothing(self):
        """notify=False 时绝不发通知。"""
        with tempfile.TemporaryDirectory() as td:
            root = _make_root(td, {"p1": "paper"})
            _touch_locks(root, "p1")
            wd = Watchdog(root, notify=False)
            wd.discover()
            with mock.patch("gate_bot.monitoring.notify_process_event") as m:
                with mock.patch.object(wd, "_spawn", return_value=True):
                    with mock.patch.object(Watchdog, "_alive", return_value=False):
                        wd.check_once()
                m.assert_not_called()

    def test_storm_card_fields(self):
        """重启风暴用 storm 红色卡片。"""
        wd = Watchdog(Path("."), max_restarts_per_hour=1, notify=True)
        t = Target("bot-x", "plan")
        calls = []
        with mock.patch(
            "gate_bot.monitoring.notify_process_event",
            side_effect=lambda **kw: calls.append(kw) or True,
        ):
            self.assertTrue(wd._allow_restart(t))
            self.assertFalse(wd._allow_restart(t))  # 触发风暴
        self.assertTrue(calls, "应发出风暴告警")
        self.assertEqual(calls[0].get("kind"), "storm")
        self.assertEqual(calls[0].get("color"), "red")


if __name__ == "__main__":
    unittest.main()
