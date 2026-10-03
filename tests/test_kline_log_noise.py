# -*- coding: utf-8 -*-
"""kline 覆盖摘要不能刷屏（日志 541 MB/天 → 96% 是同一行）。

背景：`flush_status()` 原来在**每次 K 线 upsert** 时都打一遍覆盖摘要 ——
而每个轮询周期要 upsert 5 品种 × 6 周期 = 30 根。实测服务器日志
**199 MB / 6 小时 ≈ 541 MB/天**，其中 **19176/20000 行（96%）是同一条 638 字符的
状态行**。而 `TimedRotatingFileHandler(backupCount=7)` 按天轮转 ⇒ 峰值可达数 GB。

改法：**真有新增才打；无变化时最多每 `STATUS_HEARTBEAT_SEC` 打一次**（保留保活信号，
否则「无输出」又会被当成「没在跑」）。

pa-data-source 不在 omnialpha 的包内，这里按路径引入。
"""
from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PA = ROOT / "pa-data-source"
if str(PA) not in sys.path:
    sys.path.insert(0, str(PA))

# `kline_watcher` 顶层 `import websocket`（pa-data-source 的依赖 websocket-client），
# 而 omnialpha 的 requirements 只有 pyyaml/matplotlib —— 跑测试的解释器里不一定有它。
# 本测试只关心 `flush_status()` 的日志节流逻辑，不需要真的 websocket，
# 所以缺了就塞个空壳让 import 过得去。
# （不这么做的话：本机恰好装了 websocket 就绿、服务器没装就整条 ImportError ——
#   实测踩到过一次，部署直接被测试失败挡住。）
try:
    import websocket  # noqa: F401
except ModuleNotFoundError:
    sys.modules["websocket"] = types.ModuleType("websocket")

import kline_watcher as kw  # noqa: E402


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class TestCoverageLogNotSpammy(unittest.TestCase):
    def setUp(self):
        self.cap = _Capture()
        kw.logger.addHandler(self.cap)
        kw.logger.setLevel(logging.INFO)
        self._orig_intervals = kw.get_symbol_intervals
        kw.get_symbol_intervals = lambda: [
            {"name": "BTC_USDT", "intervals": ["1m", "5m"]},
            {"name": "ETH_USDT", "intervals": ["1m", "5m"]},
        ]
        kw.status_counters.clear()
        kw._last_status_log = 0.0

    def tearDown(self):
        kw.logger.removeHandler(self.cap)
        kw.get_symbol_intervals = self._orig_intervals
        kw.status_counters.clear()
        kw._last_status_log = 0.0

    def _advance(self):
        """把「上次打点时间」推到一个心跳间隔之前，模拟时间流逝。"""
        kw._last_status_log = kw.time.time() - (kw.STATUS_HEARTBEAT_SEC + 1)

    def test_rapid_flushes_are_rate_limited(self):
        """高频调用只应留下第一次，其余被限频吞掉。"""
        for _ in range(20):
            kw.flush_status()
        self.assertEqual(
            len(self.cap.lines), 1,
            f"20 次连续 flush 只该打 1 次，实际 {len(self.cap.lines)} 次",
        )

    def test_liveness_even_without_any_change(self):
        """**没有变化也要按心跳打** —— 否则「无输出」会被当成「没在跑」。"""
        kw.flush_status()
        self.cap.lines.clear()
        self._advance()
        kw.flush_status()
        self.assertEqual(len(self.cap.lines), 1, "到点应打一次保活，即使没有任何新增")

    def test_delta_accumulates_across_the_window(self):
        """窗口内多次新增要累加到同一条 `+N` 里 —— 提前 return 不能清计数器。"""
        kw.flush_status()                 # 窗口起点
        self.cap.lines.clear()
        kw.update_status("BTC_USDT", "1m", "add", 2000)
        kw.update_status("BTC_USDT", "1m", "add", 2001)
        kw.flush_status()                 # 被限频吞掉
        self.assertEqual(self.cap.lines, [], "限频窗口内不该输出")
        kw.update_status("BTC_USDT", "1m", "add", 2002)
        self._advance()
        kw.flush_status()                 # 到点
        self.assertEqual(len(self.cap.lines), 1)
        self.assertIn("BTC_USDT 1m +3 (2002)", self.cap.lines[0])

    def test_real_cadence_bounds_daily_volume(self):
        """核心指标：一天最多打 86400/STATUS_HEARTBEAT_SEC 条。"""
        per_day = int(86400 / kw.STATUS_HEARTBEAT_SEC)
        self.assertLessEqual(per_day, 200, f"每天 {per_day} 条太多（原实现是 ~73 万条）")


if __name__ == "__main__":
    unittest.main()
