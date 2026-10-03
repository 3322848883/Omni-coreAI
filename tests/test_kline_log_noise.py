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

    def test_unchanged_repeated_flushes_are_suppressed(self):
        """无变化时反复 flush 只应留下第一次（心跳起点），不是每次都打。"""
        for _ in range(20):
            kw.flush_status()
        self.assertEqual(
            len(self.cap.lines), 1,
            f"20 次无变化的 flush 只该打 1 次（首次心跳），实际 {len(self.cap.lines)} 次",
        )

    def test_change_is_logged_with_delta(self):
        kw.flush_status()                      # 首次心跳
        self.cap.lines.clear()
        kw.update_status("BTC_USDT", "1m", "add", 2000)
        kw.update_status("BTC_USDT", "1m", "add", 2001)
        kw.flush_status()
        self.assertEqual(len(self.cap.lines), 1)
        self.assertIn("BTC_USDT 1m +2 (2001)", self.cap.lines[0])

    def test_heartbeat_resumes_after_interval(self):
        kw.flush_status()                      # 首次
        self.cap.lines.clear()
        kw._last_status_log = kw.time.time() - (kw.STATUS_HEARTBEAT_SEC + 1)
        kw.flush_status()
        self.assertEqual(len(self.cap.lines), 1, "超过心跳间隔后应重新打一次保活")

    def test_change_resets_counter_so_nothing_lost(self):
        kw.update_status("ETH_USDT", "5m", "add", 10)
        kw.flush_status()
        self.cap.lines.clear()
        kw.flush_status()                      # 无变化 → 抑制
        self.assertEqual(self.cap.lines, [])
        kw.update_status("ETH_USDT", "5m", "add", 11)
        kw.flush_status()
        self.assertIn("+1 (11)", self.cap.lines[0])


if __name__ == "__main__":
    unittest.main()
