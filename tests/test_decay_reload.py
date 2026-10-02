# -*- coding: utf-8 -*-
"""衰减检测（DecayDetector）回归测试。

背景：`PlanRunner._record_decay()` 的两个调用点都传 `pnl_usd=0.0`（硬编码），
而且 `DecayDetector._history` 是**实例状态**、从不从盘上恢复 —— 而 plan-loop
**每轮都 new 一个 DecayDetector**。两者叠加的结果是：

- `_history` 永远只有 1 条 → `check()` 要求 `len >= window`(默认 20) → **永远返回 None**
- `n_trades` / `win_rate` / `rolling_pnl` / `rolling_sharpe` 恒为 0

即**衰减告警永远不可能触发**，与 HealthMonitor 当初「实例内计数、每轮新建实例」是同一类问题。
实测：全部 35 个 bot 的 `state/perf_metrics.jsonl` 最后一行都是 `{'win_rate': 0, 'n_trades': 0}`。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.monitoring import DecayDetector  # noqa: E402
from omnialpha.strategist.loop import PlanRunner  # noqa: E402


class TestCrossInstanceHistory(unittest.TestCase):
    def test_history_survives_new_instance(self):
        """核心回归：每轮 new 一个 detector，也要能累积到 window 并产出非零指标。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(25):
                det = DecayDetector(root, "b", window=20)
                det.record_cycle(cycle_id=f"c{i}", decision="hold", executed=True,
                                 pnl_usd=(1.0 if i % 2 else -1.0))
            fresh = DecayDetector(root, "b", window=20)
            self.assertGreaterEqual(len(fresh._history), 20)
            m = fresh._compute()
            self.assertGreater(m["n_trades"], 0, "跨实例恢复后 n_trades 应非 0")
            self.assertGreater(m["win_rate"], 0, "跨实例恢复后 win_rate 应非 0")

    def test_check_can_fire_after_enough_cycles(self):
        """连续亏损到足够轮数后，check() 应该能产出胜率告警（而不是永远 None）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(25):
                det = DecayDetector(root, "b", window=20)
                det.record_cycle(cycle_id=f"c{i}", decision="open_long",
                                 executed=True, pnl_usd=-1.0)
            alert = DecayDetector(root, "b", window=20).check()
            self.assertIsNotNone(alert, "连续亏损后应触发衰减告警")
            self.assertTrue(any("win_rate" in a for a in alert["alerts"]))

    def test_no_file_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            det = DecayDetector(Path(td), "never-ran")
            self.assertEqual(det._history, [])


class TestEquityDeltaPnl(unittest.TestCase):
    def test_equity_delta_used_when_pnl_missing(self):
        """pnl 缺省时用权益差推算；首轮无上次权益则记 0。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r1 = DecayDetector(root, "b").record_cycle(
                cycle_id="c1", decision="hold", executed=True, equity=100.0)
            self.assertEqual(r1["pnl_usd"], 0.0)
            self.assertEqual(r1["equity"], 100.0)

            # 新实例（模拟下一轮）也要记得上次权益
            r2 = DecayDetector(root, "b").record_cycle(
                cycle_id="c2", decision="hold", executed=True, equity=98.5)
            self.assertAlmostEqual(r2["pnl_usd"], -1.5, places=6)

    def test_explicit_pnl_wins(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = DecayDetector(root, "b").record_cycle(
                cycle_id="c1", decision="hold", executed=True,
                pnl_usd=7.0, equity=100.0)
            self.assertEqual(r["pnl_usd"], 7.0)

    def test_no_equity_no_pnl_is_zero(self):
        with tempfile.TemporaryDirectory() as td:
            r = DecayDetector(Path(td), "b").record_cycle(
                cycle_id="c1", decision="hold", executed=True)
            self.assertEqual(r["pnl_usd"], 0.0)


class TestSnapshotEquity(unittest.TestCase):
    def test_extracts_total_then_balance(self):
        self.assertAlmostEqual(PlanRunner._snapshot_equity({"account": {"total": "88.06"}}), 88.06)
        self.assertAlmostEqual(PlanRunner._snapshot_equity({"account": {"balance": 12.5}}), 12.5)

    def test_missing_is_none(self):
        self.assertIsNone(PlanRunner._snapshot_equity({}))
        self.assertIsNone(PlanRunner._snapshot_equity({"account": {}}))
        self.assertIsNone(PlanRunner._snapshot_equity(None))
        self.assertIsNone(PlanRunner._snapshot_equity({"account": {"total": "n/a"}}))


if __name__ == "__main__":
    unittest.main()
