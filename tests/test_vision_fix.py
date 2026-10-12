"""vision 图片生成修复的回归测试（2026-10-13）。

覆盖三个修复点：
1. collect_snapshot 把 vision_timeframes 并入 extra 拉取 —— 否则 vision 配了
   5m 而 market.timeframes 没配时，5m 每轮 no_candles（实盘 brooks-eth 实测）。
2. generate_and_encode 异常上抛（不再静默吞成 reason="empty"）—— 排查时
   根因（运行环境缺 Pillow）完全不可见，被迫逐层猜。
3. 异常上抛后 loop._generate_charts 逐 tf try 捕获，缺图不炸整轮。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestVisionTfMerge(unittest.TestCase):
    """vision_timeframes 并入 snapshot 的 extra_timeframes 拉取。"""

    def _fake_client(self):
        c = MagicMock()
        c.get_ticker.return_value = {"last": "100"}
        c.get_account.return_value = {"available": "1", "total": "1"}
        # 主周期 + 每个 extra tf 都给 30 根蜡烛
        def fake_candles(symbol, interval, limit):
            return [
                {"t": 1700000000 + i * 60, "o": 100 + i, "h": 101 + i,
                 "l": 99 + i, "c": 100.5 + i, "v": 10}
                for i in range(30)
            ]
        c.get_klines.side_effect = fake_candles
        return c

    def _collect(self, extra_tfs, vision_tfs, interval="15m"):
        from omnialpha.strategist.market import MarketConfig
        from omnialpha.strategist.snapshot import collect_snapshot

        cfg = MarketConfig(extra_timeframes=list(extra_tfs))
        snap = collect_snapshot(
            self._fake_client(), ["ETH_USDT"],
            candles=30, interval=interval, market_cfg=cfg,
            env="paper", vision_timeframes=list(vision_tfs or []),
        )
        return snap

    def test_vision_tf_gets_candles(self):
        """vision 配了 5m（市场层没配）→ snapshot.tf 里有 5m 蜡烛。"""
        snap = self._collect(["1h"], ["5m", "15m", "1h"])
        m = snap["market"]["ETH_USDT"]
        self.assertIn("5m", m.get("tf") or {})
        self.assertGreaterEqual(len(m["tf"]["5m"]["candles"]), 10)
        self.assertIn("1h", m.get("tf") or {})

    def test_no_vision_tfs_unchanged(self):
        """不传 vision_timeframes → 行为与旧版一致（只有 extra_timeframes）。"""
        snap = self._collect(["1h"], None)
        m = snap["market"]["ETH_USDT"]
        self.assertIn("1h", m.get("tf") or {})
        self.assertNotIn("5m", m.get("tf") or {})

    def test_vision_tf_dedup_with_main_interval(self):
        """vision 主周期与 interval 相同时不重复拉。"""
        snap = self._collect(["1h"], ["15m"])
        m = snap["market"]["ETH_USDT"]
        self.assertNotIn("15m", m.get("tf") or {})  # 主周期走 candles，不进 tf
        self.assertIn("1h", m.get("tf") or {})


class TestGenerateAndEncodeRaises(unittest.TestCase):
    """generate_and_encode 异常不再被静默吞掉。"""

    def test_exception_propagates(self):
        from omnialpha.strategist import vision

        with patch.object(vision, "generate_candlestick_chart",
                          side_effect=ImportError("No module named 'PIL'")):
            with self.assertRaises(ImportError):
                vision.generate_and_encode(
                    [{"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}] * 30,
                    symbol="ETH_USDT", timeframe="15m",
                )

    def test_loop_generate_charts_catches_and_records(self):
        """loop._generate_charts 逐 tf 捕获 → meta 记异常文本，整轮不炸。"""
        from omnialpha.strategist import vision
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        cfg = StrategistConfig(
            symbols=["ETH_USDT"], timeframe="15m",
            vision_timeframes=["15m"],
        )
        runner = PlanRunner.__new__(PlanRunner)
        runner.cfg = cfg
        runner._last_chart_meta = []
        snapshot = {"market": {"ETH_USDT": {"candles": [
            {"t": 1700000000 + i * 900, "o": 100 + i, "h": 101 + i,
             "l": 99 + i, "c": 100.5 + i, "v": 10} for i in range(30)
        ]}}}
        with patch.object(vision, "generate_and_encode",
                          side_effect=ImportError("No module named 'PIL'")):
            out = runner._generate_charts(snapshot)  # 不应抛出
        self.assertEqual(out, [])
        self.assertEqual(len(runner._last_chart_meta), 1)
        meta = runner._last_chart_meta[0]
        self.assertFalse(meta["ok"])
        self.assertIn("PIL", meta["reason"])


if __name__ == "__main__":
    unittest.main()
