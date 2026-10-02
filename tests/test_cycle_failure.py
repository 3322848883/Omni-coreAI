# -*- coding: utf-8 -*-
"""plan 周期失败链路：降级 hold + 连续失败可见性。

覆盖实盘那 30% 周期失败长时间无人察觉的两处根因：
  1. HealthMonitor 每次调用都是新实例 → 连续失败数从未落盘，heartbeat 永远写 0
  2. run_once 失败直接返回 ok=False → 既不记录也不告警
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.monitoring import TYPE_PLAN_FAIL, HealthMonitor, read_alerts
from omnialpha.strategist.llm_client import LLMError
from omnialpha.strategist.loop import PlanRunner, StrategistConfig

GOOD_PLAN = (
    '{"cycle_id":"c1","reasoning":"r",'
    '"chips":[{"symbol":"BTC_USDT","action":"hold","confidence":0.0}]}'
)


def _health(root: Path, bot: str = "bot-a") -> dict:
    return json.loads(
        (root / "data" / "bots" / bot / "state" / "health.json").read_text(encoding="utf-8")
    )


class TestHealthPersistence(unittest.TestCase):
    """调用方普遍写成 HealthMonitor(root, bid).record_error()，状态必须落盘。"""

    def test_error_streak_survives_new_instance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(3):
                HealthMonitor(root, "bot-a").record_error()
            self.assertEqual(HealthMonitor(root, "bot-a").record_error(), 4)

    def test_streak_visible_in_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(6):
                HealthMonitor(root, "bot-a").record_error()
            h = HealthMonitor(root, "bot-a")
            h.heartbeat(llm_latency=1.0)
            self.assertTrue(any("error_streak" in a for a in h.check()))
            self.assertEqual(_health(root)["error_streak"], 6)

    def test_record_success_resets_across_instances(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(2):
                HealthMonitor(root, "bot-a").record_error()
            HealthMonitor(root, "bot-a").record_success()
            h = HealthMonitor(root, "bot-a")
            h.heartbeat(llm_latency=1.0)
            self.assertEqual(h.check(), [])

    def test_streak_alert_lands_in_alerts_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(5):
                HealthMonitor(root, "bot-a").record_error(detail="parse_failed: x")
            alerts = read_alerts(root, "bot-a", TYPE_PLAN_FAIL)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["streak"], 5)

    def test_no_alert_below_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for _ in range(4):
                HealthMonitor(root, "bot-a").record_error()
            self.assertEqual(read_alerts(root, "bot-a", TYPE_PLAN_FAIL), [])

    def test_latency_alerts_still_work(self):
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a")
            h.heartbeat(llm_latency=200.0)
            self.assertTrue(any("llm_latency" in a for a in h.check()))


class TestHoldPlan(unittest.TestCase):
    def test_shape(self):
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=["BTC_USDT"])
        p = r._hold_plan("c1", "llm_failed: 502")
        self.assertEqual(p.cycle_id, "c1")
        self.assertEqual(p.chips[0].action, "hold")
        self.assertTrue(p.reasoning.startswith("[降级]"))


class TestRunOnceDegrade(unittest.TestCase):
    """失败不再让整轮缺席：降级 hold + 记失败；成功则清零。"""

    def _runner(self, td: str, chat):
        cfg = StrategistConfig(
            symbols=["BTC_USDT"],
            vision=False,
            prompt_file="",           # 用默认策略文本，不依赖 prompts/ 目录
            bot_root=Path(td),
            bot_id="bot-a",
            env="paper",              # paper：不推飞书
            write_hold=True,
        )
        r = PlanRunner(
            client=object(), cfg=cfg,
            inbox=Path(td) / "inbox", history_dir=Path(td) / "state",
            llm=object(),
        )
        r._chat_with_tools = chat
        return r

    def _run(self, td: str, chat):
        from omnialpha.strategist import loop as loopmod

        r = self._runner(td, chat)
        with mock.patch.object(loopmod, "collect_snapshot", return_value={}), \
             mock.patch.object(loopmod, "build_system_prompt", return_value="sys"), \
             mock.patch.object(loopmod, "build_user_prompt", return_value="usr"):
            return r.run_once(trigger="test")

    def test_llm_failure_degrades_to_hold(self):
        with tempfile.TemporaryDirectory() as td:
            def boom(*_a, **_kw):
                raise LLMError("502 bad gateway")

            res = self._run(td, boom)
            self.assertTrue(res["ok"], "失败不该让整轮 ok=False")
            self.assertIn("llm_failed", res["degraded"])
            self.assertEqual(res["orders"], 0)
            self.assertEqual(_health(Path(td))["error_streak"], 1)
            inbox = Path(td) / "inbox"
            self.assertEqual(sorted(inbox.glob("*.json")) if inbox.exists() else [], [])

    def test_parse_failure_degrades_to_hold(self):
        with tempfile.TemporaryDirectory() as td:
            res = self._run(td, lambda *_a, **_kw: "我不会给你 JSON，只有一段散文。")
            self.assertTrue(res["ok"])
            self.assertIn("parse_failed", res["degraded"])
            self.assertEqual(res["orders"], 0)
            self.assertEqual(_health(Path(td))["error_streak"], 1)

    def test_success_resets_streak(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            HealthMonitor(root, "bot-a").record_error()
            res = self._run(td, lambda *_a, **_kw: GOOD_PLAN)
            self.assertTrue(res["ok"])
            self.assertNotIn("degraded", res)
            h = HealthMonitor(root, "bot-a")
            h.heartbeat(llm_latency=1.0)
            self.assertEqual(h.check(), [])

    def test_failure_alert_is_guarded_for_paper(self):
        """paper 环境不得推真实飞书（测试泄漏防护）。"""
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(td, lambda *_a, **_kw: GOOD_PLAN)
            with mock.patch("omnialpha.monitoring.notify_process_event") as notify:
                for _ in range(5):
                    r._record_cycle_failure(Path(td), "bot-a", "llm_failed: x")
                notify.assert_not_called()

    def test_failure_alert_pushes_once_at_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(td, lambda *_a, **_kw: GOOD_PLAN)
            r.cfg.env = "live"
            with mock.patch("omnialpha.monitoring.notify_process_event") as notify:
                for _ in range(5):
                    r._record_cycle_failure(Path(td), "bot-a", "llm_failed: x")
                self.assertEqual(notify.call_count, 1)


if __name__ == "__main__":
    unittest.main()
