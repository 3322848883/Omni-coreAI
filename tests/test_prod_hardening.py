"""prod-hardening 测试：decay + order-state + monitor + vol-sizing + backtest。"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.backtest import (  # noqa: E402
    BacktestReplayer,
    buy_and_hold_pnl,
    deflated_sharpe,
    sma_crossover_pnl,
    t_test_pvalue,
)
from gate_bot.monitoring import DecayDetector, HealthMonitor  # noqa: E402
from gate_bot.schema import (  # noqa: E402
    MMP_CANCELED,
    ORDER_FINAL_STATES,
    PARTIAL_FILL_STATUSES,
)
from gate_bot.sizing import vol_adjust_size  # noqa: E402


class TestDecayDetector(unittest.TestCase):
    def test_record_and_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-a", window=5)
            for i in range(5):
                rec = d.record_cycle(f"c-{i}", "long", True, pnl_usd=10.0 if i % 2 == 0 else -5.0)
            self.assertEqual(rec["n_trades"], 5)
            self.assertGreater(rec["win_rate"], 0)

    def test_no_alert_when_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-a", window=5)
            for i in range(10):
                d.record_cycle(f"c-{i}", "long", True, pnl_usd=10.0)
            self.assertIsNone(d.check())

    def test_alert_on_decaying_sharpe(self):
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-a", window=5)
            # 先建立好历史（有波动的正收益）
            for i in range(10):
                d.record_cycle(f"c-{i}", "long", True, pnl_usd=50.0 + (i % 5) * 10)
            # 然后连续亏损
            for i in range(10, 20):
                d.record_cycle(f"c-{i}", "long", True, pnl_usd=-80.0 - (i % 3) * 10)
            alert = d.check()
            self.assertIsNotNone(alert, "应检测到衰减")

    def test_perf_metrics_written(self):
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-a", window=5)
            d.record_cycle("c-1", "hold", False, pnl_usd=0)
            path = Path(td) / "data" / "bots" / "bot-a" / "state" / "perf_metrics.jsonl"
            self.assertTrue(path.exists())

    def test_empty_history_no_alert(self):
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-a", window=20)
            self.assertIsNone(d.check())


class TestHealthMonitor(unittest.TestCase):
    def test_heartbeat_and_check(self):
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a")
            h.heartbeat(llm_latency=5.0, exec_latency=1.0)
            self.assertEqual(h.check(), [])

    def test_llm_latency_alert(self):
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a")
            h.heartbeat(llm_latency=200.0)
            alerts = h.check()
            self.assertTrue(any("llm_latency" in a for a in alerts))

    def test_error_streak_alert(self):
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a")
            for _ in range(6):
                h.record_error()
            h.heartbeat(llm_latency=1.0)
            alerts = h.check()
            self.assertTrue(any("error_streak" in a for a in alerts))

    def test_no_heartbeat_alert(self):
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a")
            alerts = h.check()
            self.assertTrue(any("no heartbeat" in a for a in alerts))

    def test_record_success_resets(self):
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a")
            h.record_error()
            h.record_success()
            h.heartbeat()
            self.assertEqual(h.check(), [])


class TestOrderState(unittest.TestCase):
    def test_final_states(self):
        self.assertIn("filled", ORDER_FINAL_STATES)
        self.assertIn("canceled", ORDER_FINAL_STATES)
        self.assertIn(MMP_CANCELED, ORDER_FINAL_STATES)

    def test_partial_fill_statuses(self):
        self.assertIn("partially_filled", PARTIAL_FILL_STATUSES)

    def test_mmp_constant(self):
        self.assertEqual(MMP_CANCELED, "mmp_canceled")


class TestVolSizing(unittest.TestCase):
    def test_high_vol_reduces_size(self):
        # ATR 4% vs target 2% → 减半
        self.assertEqual(vol_adjust_size(1000, 4.0, 2.0), 500.0)

    def test_low_vol_increases_size(self):
        # ATR 1% vs target 2% → 加倍（clamp 到 2x）
        self.assertEqual(vol_adjust_size(1000, 1.0, 2.0), 2000.0)

    def test_clamp_prevents_extremes(self):
        # ATR 0.1% → ratio=20 → clamp 2x
        self.assertEqual(vol_adjust_size(1000, 0.1, 2.0), 2000.0)
        # ATR 20% → ratio=0.1 → clamp 0.5x
        self.assertEqual(vol_adjust_size(1000, 20.0, 2.0), 500.0)

    def test_zero_target_disables(self):
        self.assertEqual(vol_adjust_size(1000, 2.0, 0), 1000)

    def test_zero_atr_safe(self):
        self.assertEqual(vol_adjust_size(1000, 0, 2.0), 1000)

    def test_none_safe(self):
        self.assertEqual(vol_adjust_size(None, 2.0, 2.0), 0)


class TestBacktestStats(unittest.TestCase):
    def test_deflated_sharpe_basic(self):
        returns = [1.0, -0.5, 1.5, -0.3, 1.2, -0.1, 0.8, -0.2, 1.1, 0.5]
        dsr = deflated_sharpe(returns, n_trials=5)
        self.assertGreater(dsr, 0)
        self.assertLessEqual(dsr, 1)

    def test_deflated_sharpe_empty(self):
        self.assertEqual(deflated_sharpe([], n_trials=1), 0.0)

    def test_t_test_pvalue(self):
        # 稳定正收益 → 低 p-value
        returns = [1.0] * 20
        p = t_test_pvalue(returns)
        self.assertLess(p, 0.05)

    def test_buy_and_hold(self):
        self.assertAlmostEqual(buy_and_hold_pnl([100, 110]), 10.0)

    def test_sma_crossover(self):
        # 先涨后跌再涨 → 交叉产生交易
        closes = [100 + i * 0.5 for i in range(100)] + [150 - i * 0.5 for i in range(100)] + [100 + i * 0.5 for i in range(100)]
        pnl = sma_crossover_pnl(closes, fast=5, slow=10)
        self.assertNotEqual(pnl, 0)


class TestBacktestReplay(unittest.TestCase):
    def test_empty_journal(self):
        with tempfile.TemporaryDirectory() as td:
            r = BacktestReplayer(Path(td), "bot-a")
            result = r.replay(days=30)
            self.assertIn("error", result)

    def test_replay_with_journal(self):
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "data" / "bots" / "bot-a" / "state" / "memory_journal.jsonl"
            jp.parent.mkdir(parents=True)
            with jp.open("w") as f:
                for i in range(10):
                    f.write(json.dumps({
                        "ts": int(time.time()), "cycle_id": f"c-{i}",
                        "decision": "long", "executed": i % 3 != 0,
                        "exec_result": {"pnl_usd": 5.0 if i % 3 != 0 else 0},
                    }) + "\n")
            r = BacktestReplayer(Path(td), "bot-a")
            result = r.replay(days=30)
            self.assertEqual(result["n_cycles"], 10)
            self.assertGreater(result["n_trades"], 0)

    def test_replay_with_benchmarks(self):
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "data" / "bots" / "bot-a" / "state" / "memory_journal.jsonl"
            jp.parent.mkdir(parents=True)
            with jp.open("w") as f:
                f.write(json.dumps({"ts": int(time.time()), "cycle_id": "c-1",
                                     "decision": "long", "executed": True,
                                     "exec_result": {"pnl_usd": 10}}) + "\n")
            r = BacktestReplayer(Path(td), "bot-a")
            closes = [100 + i for i in range(250)]
            result = r.replay(days=30, closes=closes)
            self.assertIn("benchmark_bh_pct", result)
            self.assertIn("benchmark_sma_pct", result)


class TestAlertNotifier(unittest.TestCase):
    """告警通知渠道测试。"""

    def test_no_channel_has_no_channel(self):
        from gate_bot.monitoring import AlertNotifier
        n = AlertNotifier()
        self.assertFalse(n.has_channel)

    def test_feishu_channel(self):
        from gate_bot.monitoring import AlertNotifier
        n = AlertNotifier(feishu_webhook='https://fake/webhook')
        self.assertTrue(n.has_channel)

    def test_telegram_channel(self):
        from gate_bot.monitoring import AlertNotifier
        n = AlertNotifier(telegram_bot_token='123:abc', telegram_chat_id='456')
        self.assertTrue(n.has_channel)

    def test_both_channels(self):
        from gate_bot.monitoring import AlertNotifier
        n = AlertNotifier(feishu_webhook='https://fake',
                          telegram_bot_token='123:abc', telegram_chat_id='456')
        self.assertTrue(n.has_channel)

    def test_send_without_channel_logs_warning(self):
        from gate_bot.monitoring import AlertNotifier
        n = AlertNotifier()
        result = n.send('test alert')
        self.assertFalse(result)

    def test_send_with_invalid_webhook(self):
        """无效 webhook 不崩溃，返回 False。"""
        from gate_bot.monitoring import AlertNotifier
        n = AlertNotifier(feishu_webhook='https://invalid.example.com/hook')
        result = n.send('test alert')
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
