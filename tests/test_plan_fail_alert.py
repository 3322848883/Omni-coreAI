"""P0：plan 连续失败告警（崩溃路径）+ 恢复通知。

事故（2026-10-26）：`schema.py` 被整文件覆盖丢 `symbols` → `parse_plan_text()` 抛
`TypeError`（**不是** `PlanError`）→ `degraded` 分支没走到 → `error_streak` 恒为 0
→ `plan_fail` 告警与飞书 15 小时一次没响，两个 bot 静默停摆。

这里的守卫：
1. `run_once` 抛任何异常都要**记一次失败并原样抛出**（不吞异常、不改语义）
2. 达到阈值（`alert_fail_streak`，默认 2）落 `plan_fail` 告警
3. 恢复时落 `plan_recovered`（且只在**此前已达阈值**时发，避免每次小抖动都发）
4. 失败计数按 bot 隔离（两个 bot 各写各的 `health.json`）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Boom(Exception):
    pass


def _runner(td, *, threshold=2, bot="t-bot"):
    """造一个只够跑 run_once 兜底的 PlanRunner。"""
    from omnialpha.strategist.loop import PlanRunner, StrategistConfig

    r = PlanRunner.__new__(PlanRunner)
    r.cfg = StrategistConfig(bot_id=bot, bot_root=Path(td), alert_fail_streak=threshold)
    r.inbox = Path(td) / "inbox"
    r.inbox.mkdir(parents=True, exist_ok=True)
    return r


def _alerts(td, bot="t-bot"):
    p = Path(td) / "data" / "bots" / bot / "state" / "alerts.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("alerts") or []


def _streak(td, bot="t-bot"):
    p = Path(td) / "data" / "bots" / bot / "state" / "health.json"
    if not p.exists():
        return 0
    return int(json.loads(p.read_text(encoding="utf-8")).get("error_streak") or 0)


class CrashPathTest(unittest.TestCase):
    def test_uncaught_exception_counts_as_failure(self):
        """核心回归：**非 PlanError 的异常**也要计入连续失败。"""
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)

            def boom(trigger="manual"):
                raise _Boom("parse_plan_text() got an unexpected keyword argument 'symbols'")

            r._run_once_impl = boom
            with self.assertRaises(_Boom):
                r.run_once("interval")
            self.assertEqual(_streak(td), 1, "崩溃必须计一次失败，否则告警永远不响")

    def test_exception_is_reraised_not_swallowed(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)

            def boom(trigger="manual"):
                raise _Boom("x")

            r._run_once_impl = boom
            with self.assertRaises(_Boom):
                r.run_once()

    def test_alert_after_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, threshold=2)

            def boom(trigger="manual"):
                raise _Boom("crash")

            r._run_once_impl = boom
            for _ in range(2):
                with self.assertRaises(_Boom):
                    r.run_once()
            types = [a.get("type") for a in _alerts(td)]
            self.assertIn("plan_fail", types, "达到阈值必须落 plan_fail 告警")
            self.assertEqual(_streak(td), 2)

    def test_no_alert_before_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, threshold=3)

            def boom(trigger="manual"):
                raise _Boom("crash")

            r._run_once_impl = boom
            for _ in range(2):
                with self.assertRaises(_Boom):
                    r.run_once()
            self.assertNotIn("plan_fail", [a.get("type") for a in _alerts(td)])

    def test_normal_return_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)
            r._run_once_impl = lambda trigger="manual": {"ok": True, "orders": 0}
            self.assertEqual(r.run_once()["ok"], True)
            self.assertEqual(_streak(td), 0)

    def test_bots_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            for bot in ("a", "b"):
                r = _runner(td, bot=bot)

                def boom(trigger="manual"):
                    raise _Boom("x")

                r._run_once_impl = boom
                with self.assertRaises(_Boom):
                    r.run_once()
            self.assertEqual(_streak(td, "a"), 1)
            self.assertEqual(_streak(td, "b"), 1)


class RecoveryTest(unittest.TestCase):
    def test_recovered_alert_after_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, threshold=2)
            r._record_cycle_failure(Path(td), "t-bot", "crash: TypeError: boom")
            r._record_cycle_failure(Path(td), "t-bot", "crash: TypeError: boom")
            r._record_cycle_success(Path(td), "t-bot")
            types = [a.get("type") for a in _alerts(td)]
            self.assertIn("plan_recovered", types, "恢复必须发正向事件")
            self.assertEqual(_streak(td), 0)

    def test_no_recovered_for_single_blip(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, threshold=3)
            r._record_cycle_success(Path(td), "t-bot")   # 从没失败过
            types = [a.get("type") for a in _alerts(td)]
            self.assertNotIn("plan_recovered", types, "没失败过就不该发恢复")


class ConfigTest(unittest.TestCase):
    def test_threshold_from_config(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_runner(td, threshold=4)._alert_threshold(), 4)

    def test_threshold_floor(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, threshold=0)
            self.assertEqual(r._alert_threshold(), 1, "0/负数要兜底成 1")

    def test_default_is_two(self):
        from omnialpha.strategist.loop import StrategistConfig

        self.assertEqual(StrategistConfig().alert_fail_streak, 2,
                         "默认 2：900s interval → 30 分钟内必响")


if __name__ == "__main__":
    unittest.main()
