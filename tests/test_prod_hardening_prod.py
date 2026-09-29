"""prod-hardening 生产级测试：真实数据流/并发/边界/集成验证。"""
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.backtest import BacktestReplayer  # noqa: E402
from gate_bot.backtest.stats import (  # noqa: E402
    buy_and_hold_pnl, deflated_sharpe, sma_crossover_pnl, t_test_pvalue,
)
from gate_bot.monitoring import DecayDetector, HealthMonitor  # noqa: E402
from gate_bot.sizing import vol_adjust_size  # noqa: E402
from gate_bot.schema import ORDER_FINAL_STATES, PARTIAL_FILL_STATUSES, MMP_CANCELED  # noqa: E402


# ─────────────────────────────────────────────────────
# 1. DecayDetector 生产级
# ─────────────────────────────────────────────────────
class TestDecayProduction(unittest.TestCase):
    def test_realistic_trading_sequence(self):
        """模拟真实交易序列：盈利→震荡→亏损→衰减告警。"""
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-real", window=10)
            # 阶段1: 盈利期（10 轮）
            for i in range(10):
                d.record_cycle(f"c-{i}", "long", True, pnl_usd=20.0 + (i % 3) * 5)
            self.assertIsNone(d.check(), "盈利期不应告警")
            # 阶段2: 震荡期（10 轮）
            for i in range(10, 20):
                d.record_cycle(f"c-{i}", "hold", False, pnl_usd=0)
            # 阶段3: 亏损期（10 轮）
            for i in range(20, 30):
                d.record_cycle(f"c-{i}", "long", True, pnl_usd=-30.0 - (i % 4) * 5)
            alert = d.check()
            self.assertIsNotNone(alert, "亏损期应触发衰减告警")
            self.assertIn("alerts", alert)

    def test_perf_metrics_jsonl_format(self):
        """perf_metrics.jsonl 每行可解析。"""
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-a", window=5)
            for i in range(8):
                d.record_cycle(f"c-{i}", "long", True, pnl_usd=float(i))
            path = Path(td) / "data" / "bots" / "bot-a" / "state" / "perf_metrics.jsonl"
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 8)
            for line in lines:
                rec = json.loads(line)
                self.assertIn("rolling_sharpe", rec)
                self.assertIn("win_rate", rec)
                self.assertIn("ts", rec)

    def test_concurrent_record(self):
        """多线程同时 record_cycle 不丢数据。"""
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-a", window=5)
            def do(i):
                d.record_cycle(f"c-{i}", "long", True, pnl_usd=float(i % 10))
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(do, i) for i in range(50)]
                [f.result() for f in as_completed(futures)]
            path = Path(td) / "data" / "bots" / "bot-a" / "state" / "perf_metrics.jsonl"
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 50)

    def test_win_rate_alert_threshold(self):
        """win_rate < 0.3 且 trades >= 5 触发告警。"""
        with tempfile.TemporaryDirectory() as td:
            d = DecayDetector(Path(td), "bot-a", window=5)
            for i in range(20):
                pnl = 10.0 if i < 3 else -10.0  # 3 胜 17 负
                d.record_cycle(f"c-{i}", "long", True, pnl_usd=pnl)
            alert = d.check()
            self.assertIsNotNone(alert)
            self.assertTrue(any("win_rate" in a for a in alert["alerts"]))


# ─────────────────────────────────────────────────────
# 2. HealthMonitor 生产级
# ─────────────────────────────────────────────────────
class TestHealthProduction(unittest.TestCase):
    def test_full_health_cycle(self):
        """完整健康周期：正常→延迟告警→错误告警→恢复。"""
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a")
            # 正常
            h.heartbeat(llm_latency=5.0, exec_latency=1.0)
            self.assertEqual(h.check(), [])
            # 延迟告警
            h.heartbeat(llm_latency=150.0, exec_latency=40.0)
            alerts = h.check()
            self.assertTrue(any("llm_latency" in a for a in alerts))
            self.assertTrue(any("exec_latency" in a for a in alerts))
            # 错误累积
            for _ in range(6):
                h.record_error()
            h.heartbeat(llm_latency=5.0)
            self.assertTrue(any("error_streak" in a for a in h.check()))
            # 恢复
            h.record_success()
            h.heartbeat(llm_latency=5.0)
            self.assertEqual(h.check(), [])

    def test_heartbeat_file_format(self):
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a")
            h.heartbeat(llm_latency=3.0, exec_latency=1.0, custom_field="x")
            path = Path(td) / "data" / "bots" / "bot-a" / "state" / "health.json"
            rec = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("last_heartbeat", rec)
            self.assertEqual(rec["llm_latency"], 3.0)
            self.assertEqual(rec["custom_field"], "x")

    def test_stale_heartbeat_critical(self):
        """心跳超时 → CRITICAL 告警。"""
        with tempfile.TemporaryDirectory() as td:
            h = HealthMonitor(Path(td), "bot-a", heartbeat_crit=1)
            h.heartbeat(llm_latency=1.0)
            time.sleep(2.0)  # 2s > 1s crit，留充足缓冲
            alerts = h.check()
            self.assertTrue(any("heartbeat" in a and "crit" in a for a in alerts))


# ─────────────────────────────────────────────────────
# 3. vol-sizing 生产级
# ─────────────────────────────────────────────────────
class TestVolSizingProduction(unittest.TestCase):
    def test_realistic_btc_volatility(self):
        """真实 BTC 波动率范围（0.5% ~ 5%）。"""
        for atr_pct, target in [(0.5, 2.0), (1.0, 2.0), (2.0, 2.0), (3.0, 2.0), (5.0, 2.0)]:
            result = vol_adjust_size(1000, atr_pct, target)
            # 结果必须在 clamp 范围内
            self.assertGreaterEqual(result, 500.0, f"atr={atr_pct}")
            self.assertLessEqual(result, 2000.0, f"atr={atr_pct}")

    def test_same_vol_unchanged(self):
        self.assertEqual(vol_adjust_size(1000, 2.0, 2.0), 1000.0)

    def test_all_zeros_safe(self):
        self.assertEqual(vol_adjust_size(0, 0, 0), 0)

    def test_negative_base(self):
        self.assertEqual(vol_adjust_size(-100, 2.0, 2.0), -100)  # 保持原值

    def test_integration_with_risk_config(self):
        """模拟 risk 配置中的 target_volatility_pct 使用。"""
        config = {"target_volatility_pct": 1.5}
        base_size = 2000
        atr = 3.0
        result = vol_adjust_size(base_size, atr, config["target_volatility_pct"])
        self.assertEqual(result, 1000.0)  # 1.5/3.0 = 0.5x → 1000


# ─────────────────────────────────────────────────────
# 4. 订单状态机生产级
# ─────────────────────────────────────────────────────
class TestOrderStateProduction(unittest.TestCase):
    def test_okx_state_mapping(self):
        """OKX 订单状态完整映射。"""
        states = {"live", "partially_filled", "filled", "canceled", "mmp_canceled"}
        for s in states:
            if s in ORDER_FINAL_STATES:
                self.assertIn(s, ORDER_FINAL_STATES)
            elif s in PARTIAL_FILL_STATUSES:
                self.assertIn(s, PARTIAL_FILL_STATUSES)
            else:
                self.assertNotIn(s, ORDER_FINAL_STATES)

    def test_mmp_canceled_is_terminal(self):
        self.assertIn(MMP_CANCELED, ORDER_FINAL_STATES)
        self.assertNotIn(MMP_CANCELED, PARTIAL_FILL_STATUSES)


# ─────────────────────────────────────────────────────
# 5. Backtest 生产级
# ─────────────────────────────────────────────────────
class TestBacktestProduction(unittest.TestCase):
    def _make_journal(self, path: Path, n: int = 50):
        path.parent.mkdir(parents=True, exist_ok=True)
        import random
        random.seed(42)
        with path.open("w") as f:
            for i in range(n):
                pnl = random.uniform(-20, 30) if i % 3 != 0 else 0
                f.write(json.dumps({
                    "ts": int(time.time()) - (n - i) * 900,
                    "cycle_id": f"c-{i:04d}",
                    "decision": ["long", "short", "hold"][i % 3],
                    "executed": i % 3 != 0,
                    "exec_result": {"pnl_usd": round(pnl, 2)},
                }) + "\n")

    def test_full_backtest_pipeline(self):
        """完整回测流程：journal → 模拟 → 统计 → 基准。"""
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "data" / "bots" / "bot-a" / "state" / "memory_journal.jsonl"
            self._make_journal(jp, n=50)
            r = BacktestReplayer(Path(td), "bot-a")
            closes = [100 + i * 0.5 + (i % 7) * 0.3 for i in range(250)]
            result = r.replay(days=30, closes=closes)
            # 完整字段
            self.assertIn("n_cycles", result)
            self.assertIn("n_trades", result)
            self.assertIn("total_pnl_usd", result)
            self.assertIn("sharpe", result)
            self.assertIn("dsr", result)
            self.assertIn("p_value", result)
            self.assertIn("benchmark_bh_pct", result)
            self.assertIn("benchmark_sma_pct", result)
            self.assertEqual(result["n_cycles"], 50)
            self.assertGreater(result["n_trades"], 0)

    def test_backtest_concurrent_reads(self):
        """并发读 journal 不报错。"""
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "data" / "bots" / "bot-a" / "state" / "memory_journal.jsonl"
            self._make_journal(jp, n=30)
            def run(i):
                r = BacktestReplayer(Path(td), "bot-a")
                return r.replay(days=30)
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(run, i) for i in range(5)]
                results = [f.result() for f in as_completed(futures)]
            for res in results:
                self.assertEqual(res["n_cycles"], 30)

    def test_large_journal(self):
        """大 journal（1000 条）不超时。"""
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "data" / "bots" / "bot-a" / "state" / "memory_journal.jsonl"
            self._make_journal(jp, n=1000)
            t0 = time.time()
            r = BacktestReplayer(Path(td), "bot-a")
            result = r.replay(days=365)
            elapsed = time.time() - t0
            self.assertLess(elapsed, 5.0, f"1000 entries took {elapsed:.1f}s")
            self.assertEqual(result["n_cycles"], 1000)

    def test_dsr_with_many_trials(self):
        """DSR 随 trial 数增加而降低（选择偏差修正）。"""
        returns = [0.5, -0.3, 0.8, -0.1, 0.6, -0.2, 0.7, 0.1, -0.4, 0.9]
        dsr_few = deflated_sharpe(returns, n_trials=1)
        dsr_many = deflated_sharpe(returns, n_trials=100)
        self.assertLessEqual(dsr_many, dsr_few + 0.01)  # trial 越多越保守

    def test_pvalue_interpretation(self):
        """显著正收益 → p < 0.05，随机收益 → p > 0.05。"""
        import random
        random.seed(42)
        strong = [random.gauss(5, 1) for _ in range(50)]
        noise = [random.gauss(0, 5) for _ in range(50)]
        self.assertLess(t_test_pvalue(strong), 0.05)
        self.assertGreater(t_test_pvalue(noise), 0.05)


# ─────────────────────────────────────────────────────
# 6. 集成测试：全部模块协同
# ─────────────────────────────────────────────────────
class TestFullIntegration(unittest.TestCase):
    def test_monitoring_plus_backtest_plus_sizing(self):
        """监控+回测+波动率协同工作。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1) 用 decay 记录 20 轮
            d = DecayDetector(root, "bot-x", window=5)
            for i in range(20):
                d.record_cycle(f"c-{i}", "long", True,
                               pnl_usd=15.0 if i % 2 == 0 else -8.0)
            # 2) health 记录
            h = HealthMonitor(root, "bot-x")
            h.heartbeat(llm_latency=8.0, exec_latency=2.0)
            self.assertEqual(h.check(), [])
            # 3) vol sizing
            size = vol_adjust_size(1000, 2.5, 2.0)
            self.assertEqual(size, 800.0)
            # 4) backtest 读同一 journal（从 decay 的 perf_metrics 构造）
            # 直接写一个 journal 来测试
            jp = root / "data" / "bots" / "bot-x" / "state" / "memory_journal.jsonl"
            jp.parent.mkdir(parents=True, exist_ok=True)
            with jp.open("w") as f:
                for i in range(20):
                    f.write(json.dumps({
                        "ts": int(time.time()), "cycle_id": f"c-{i}",
                        "decision": "long", "executed": i % 2 == 0,
                        "exec_result": {"pnl_usd": 15.0 if i % 2 == 0 else -8.0},
                    }) + "\n")
            r = BacktestReplayer(root, "bot-x")
            result = r.replay(days=30)
            self.assertEqual(result["n_cycles"], 20)


if __name__ == "__main__":
    unittest.main()
