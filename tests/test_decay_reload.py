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


class TestDecayEquity(unittest.TestCase):
    def test_flat_returns_equity(self):
        self.assertAlmostEqual(
            PlanRunner._decay_equity({"account": {"total": "88.06", "positions": []}}), 88.06)
        # 老 snapshot 没有 positions 键 → 视为空仓
        self.assertAlmostEqual(PlanRunner._decay_equity({"account": {"balance": 12.5}}), 12.5)

    def test_open_position_returns_none(self):
        """持仓中不给权益：否则未实现盈亏会被当成已实现、让衰减提前触发。"""
        snap = {"account": {"total": "88.06",
                            "positions": [{"contract": "BTC_USDT", "size": 19}]}}
        self.assertIsNone(PlanRunner._decay_equity(snap))

    def test_missing_or_error_is_none(self):
        self.assertIsNone(PlanRunner._decay_equity({}))
        self.assertIsNone(PlanRunner._decay_equity({"account": {}}))
        self.assertIsNone(PlanRunner._decay_equity(None))
        self.assertIsNone(PlanRunner._decay_equity({"account": {"total": "n/a"}}))
        self.assertIsNone(PlanRunner._decay_equity(
            {"account": {"total": "88.06", "error": "account: down"}}))


class TestFlatWindowIsNotDecay(unittest.TestCase):
    """空窗口（窗口内无盈亏变动）不该报衰减 —— 那是必然报警，不是信号。

    机制：`_compute()` 里 `std == 0` 时 `sharpe = 0`（窗口全 0 → 必然 std=0），
    而 `check()` 的判据是 `hist_sharpe > 0 and roll_sharpe < 0.5*hist_sharpe`
    —— `0 < 0.5×正数` 恒成立 → **每轮都报**。

    实盘佐证（2026-10-07，10 小时）：440 条 decay 告警占 1612 轮的 27%，其中
    `n_trades: 0` 出现 381 次、`rolling_sharpe: 0` 出现 381 次。

    同一个 `check()` 里 `win_rate` 那条早有 `n_trades >= 5` 的样本保护，sharpe 这条漏了。
    """

    def _det(self, td: Path) -> DecayDetector:
        return DecayDetector(Path(td), "t", window=20)

    def test_flat_window_does_not_alert(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            det = self._det(td)
            # 先来 40 笔有盈亏的（让历史 sharpe > 0）
            for i in range(40):
                det.record_cycle(cycle_id=f"h{i}", decision="open_long",
                                 executed=True, pnl_usd=1.5 + (0.5 if i % 2 else -0.5))
            # 再来 20 轮空窗口（盈亏全 0 = 没有成交）
            for i in range(20):
                det.record_cycle(cycle_id=f"z{i}", decision="hold",
                                 executed=False, pnl_usd=0.0)
            hist = det._historical()
            self.assertGreater(hist.get("sharpe", 0), 0, "前置条件：历史 sharpe 应为正")
            self.assertEqual(det._compute()["n_trades"], 0, "前置条件：窗口内应无成交")
            self.assertIsNone(det.check(), "空窗口不该被判为策略衰减")

    def test_real_decay_still_alerts(self):
        """配对：真有盈亏且滚动 sharpe 掉下来时，仍必须报警 —— 别把告警磨平。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            det = self._det(td)
            for i in range(40):
                det.record_cycle(cycle_id=f"h{i}", decision="open_long",
                                 executed=True, pnl_usd=2.0 + (0.5 if i % 2 else -0.5))
            # 窗口内全是亏损（有成交、有盈亏变动）
            for i in range(20):
                det.record_cycle(cycle_id=f"l{i}", decision="open_long",
                                 executed=True, pnl_usd=-1.0 - (0.2 if i % 2 else 0.0))
            self.assertGreater(det._compute()["n_trades"], 0, "前置条件：窗口内应有成交")
            self.assertIsNotNone(det.check(), "真衰减仍要报")


if __name__ == "__main__":
    unittest.main()
