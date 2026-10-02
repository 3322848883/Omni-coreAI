# -*- coding: utf-8 -*-
"""第 4+5 条：HealthMonitor.check() 接线 + exec_latency 落到 run 侧记录。

背景：
- `check()` 此前**全仓库无人调用** → 心跳超时 / llm_latency / exec_latency /
  error_streak 四类阈值全部形同虚设。现由看门狗周期调用（带去重）。
- `exec_latency` 之前恒为 0（plan-loop 硬编码）。而 plan-loop 与 run 是两个进程，
  `heartbeat()` 是整体覆盖语义，共用 health.json 会互相抹掉字段 → run 侧单独写
  `health.run.json`，`check()` 合并读。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from omnialpha.monitoring import HealthMonitor
from omnialpha.watchdog import Target, Watchdog


def _read(root: Path, bot: str, name: str) -> dict:
    p = Path(root) / "data" / "bots" / bot / "state" / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


class TestRoleSplit(unittest.TestCase):
    def test_plan_writes_health_json(self):
        with tempfile.TemporaryDirectory() as td:
            HealthMonitor(Path(td), "bot-a").heartbeat(llm_latency=5.0)
            self.assertTrue(_read(Path(td), "bot-a", "health.json"))
            self.assertEqual(_read(Path(td), "bot-a", "health.run.json"), {})

    def test_run_writes_health_run_json(self):
        with tempfile.TemporaryDirectory() as td:
            HealthMonitor(Path(td), "bot-a", role="run").heartbeat(exec_latency=12.0)
            self.assertEqual(_read(Path(td), "bot-a", "health.json"), {})
            self.assertEqual(_read(Path(td), "bot-a", "health.run.json")["exec_latency"], 12.0)

    def test_check_merges_run_side(self):
        """run 侧的 exec_latency 必须能被 plan 侧的 check() 看到。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a").heartbeat(llm_latency=5.0)
            HealthMonitor(root, "bot-a", role="run").heartbeat(exec_latency=99.0)
            alerts = HealthMonitor(root, "bot-a").check()
            self.assertTrue(any("exec_latency" in a for a in alerts), alerts)

    def test_plan_fields_not_clobbered_by_run(self):
        """两边分开写 → 谁也不抹掉谁。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a").heartbeat(llm_latency=5.0)
            HealthMonitor(root, "bot-a", role="run").heartbeat(exec_latency=1.0)
            self.assertEqual(_read(root, "bot-a", "health.json")["llm_latency"], 5.0)
            self.assertEqual(_read(root, "bot-a", "health.run.json")["exec_latency"], 1.0)

    def test_no_heartbeat_when_both_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIn("no heartbeat yet", HealthMonitor(Path(td), "bot-a").check()[0])

    def test_run_role_check_does_not_merge_plan(self):
        """run 侧实例只读自己的文件（不反向合并）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a").heartbeat(llm_latency=200.0)
            HealthMonitor(root, "bot-a", role="run").heartbeat(exec_latency=1.0)
            alerts = HealthMonitor(root, "bot-a", role="run").check()
            self.assertFalse(any("llm_latency" in a for a in alerts), alerts)


class TestWatcherRecordsExecLatency(unittest.TestCase):
    def test_record_writes_run_file(self):
        from omnialpha.watcher import _record_exec_latency

        class Bot:
            bot_id = "bot-a"

        class Paths:
            root = None

        with tempfile.TemporaryDirectory() as td:
            Paths.root = Path(td)
            _record_exec_latency(Bot(), Paths(), 7.5)
            rec = _read(Path(td), "bot-a", "health.run.json")
            self.assertEqual(rec["exec_latency"], 7.5)

    def test_record_never_raises(self):
        from omnialpha.watcher import _record_exec_latency

        class Bad:
            bot_id = "bot-a"

        class BadPaths:
            root = Path("Z:/definitely/not/here")

        _record_exec_latency(Bad(), BadPaths(), 1.0)  # 不抛即可


class TestWatchdogHealthWiring(unittest.TestCase):
    def _wd(self, td: str, bot: str = "bot-a") -> Watchdog:
        wd = Watchdog(Path(td), notify=False)
        wd.targets = [Target(bot, "plan")]
        return wd

    def test_alerts_on_latency_and_dedups(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a").heartbeat(llm_latency=200.0)
            wd = self._wd(td)
            first = wd._check_health()
            self.assertTrue(any("llm_latency" in a for a in first), first)
            # 去重窗口内不再重复推
            wd._last_health_check = 0.0
            self.assertEqual(wd._check_health(), [])

    def test_respects_interval(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a").heartbeat(llm_latency=200.0)
            wd = self._wd(td)
            wd._check_health()
            wd._last_health_check = time.time()          # 刚查过
            self.assertEqual(wd._check_health(), [], "间隔未到不应重复体检")

    def test_skips_no_heartbeat_yet(self):
        with tempfile.TemporaryDirectory() as td:
            wd = self._wd(td)
            self.assertEqual(wd._check_health(), [], "刚启动无心跳不算异常")

    def test_check_once_reports_health_alerts(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a").heartbeat(llm_latency=200.0)
            wd = self._wd(td)
            # 假装进程活着，避免 check_once 真去 spawn（测试里不该拉起真进程）
            with mock.patch.object(Watchdog, "_alive", return_value=True):
                out = wd.check_once()
            self.assertIn("health_alerts", out)
            self.assertTrue(any("llm_latency" in a for a in out["health_alerts"]))

    def test_healthy_bot_produces_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a").heartbeat(llm_latency=5.0, exec_latency=1.0)
            HealthMonitor(root, "bot-a", role="run").heartbeat(exec_latency=1.0)
            self.assertEqual(self._wd(td)._check_health(), [])


class TestExecFailStreak(unittest.TestCase):
    """run 侧独有的信号：连续执行失败。"""

    def test_increments_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            hm = HealthMonitor(Path(td), "bot-a", role="run")
            self.assertEqual(hm.record_exec_result(1), 1)
            self.assertEqual(hm.record_exec_result(3), 2)

    def test_resets_on_clean_batch(self):
        with tempfile.TemporaryDirectory() as td:
            hm = HealthMonitor(Path(td), "bot-a", role="run")
            hm.record_exec_result(1)
            hm.record_exec_result(1)
            self.assertEqual(hm.record_exec_result(0), 0)

    def test_persists_across_instances(self):
        """每批都 new 一个实例（watcher 就是这么用的），所以必须落盘续算。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(3):
                HealthMonitor(root, "bot-a", role="run").record_exec_result(1)
            self.assertEqual(_read(root, "bot-a", "health.run.json")["exec_fail_streak"], 3)

    def test_check_alerts_at_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(5):
                HealthMonitor(root, "bot-a", role="run").record_exec_result(1)
            alerts = HealthMonitor(root, "bot-a").check()
            self.assertTrue(any("exec_fail_streak" in a for a in alerts), alerts)


class TestStreakIsPerRole(unittest.TestCase):
    """跨进程不能互相抹掉 streak —— 这是「跨实例不落盘」那个 bug 的另一个入口。"""

    def test_error_streak_isolated_by_role(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(3):
                HealthMonitor(root, "bot-a").record_error()
            for _ in range(2):
                HealthMonitor(root, "bot-a", role="run").record_error()
            self.assertEqual(_read(root, "bot-a", "health.json")["error_streak"], 3)
            self.assertEqual(_read(root, "bot-a", "health.run.json")["error_streak"], 2)

    def test_plan_success_does_not_clear_run_streak(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a", role="run").record_error()
            HealthMonitor(root, "bot-a").record_success()
            self.assertEqual(_read(root, "bot-a", "health.run.json")["error_streak"], 1)


if __name__ == "__main__":
    unittest.main()
