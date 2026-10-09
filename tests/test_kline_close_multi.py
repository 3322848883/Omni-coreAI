# -*- coding: utf-8 -*-
"""K 线收盘事件必须覆盖**整个品种宇宙**（B-12 / T2）。

回归背景（2026-10-09 全量审计 B-12）：`PlanLoop._kline_closed` 只看 `symbols[0]`，
于是多币 bot 的第 2..N 个币的收盘事件**永远唤不醒** —— 而
`docs/compose/spec/llm-strategist.md:119` 还把这个行为写成了「规格」，
所以它既没被测试拦住、也没被文档标成缺陷。

两个方向都要钉住：

1. 多币：宇宙内**任一币**收盘即唤醒；触发标签带 symbol（`kline_close:<SYMBOL>`）；
   某币取数失败只跳过它自己；同一分钟多币同时收盘只唤醒一次。
2. 单币：行为**逐字不变**（标签仍是 `kline_close`，日志里就是 `plan[kline_close]`）。

`test_legacy_first_symbol_judgement_would_miss` 把旧判据装回去，证明这些用例
真能抓住 bug（否则就是假绿）。
"""
from __future__ import annotations

import unittest
from unittest import mock

from omnialpha.strategist.loop import PlanRunner, StrategistConfig


class _CandleClient:
    """按 symbol 给不同的最新收盘时间戳；`ts[sym] is None` = 取数失败。"""

    def __init__(self, ts: dict):
        self.ts = dict(ts)

    def get_klines(self, symbol, interval, limit=100):
        t = self.ts.get(symbol)
        if t is None:
            raise RuntimeError("kline source down")
        return [{"t": t, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0}]


def _runner(client, symbols, **kw) -> PlanRunner:
    r = PlanRunner.__new__(PlanRunner)
    r.cfg = StrategistConfig(symbols=list(symbols), timeframe="15m",
                             event_timeframe="15m", **kw)
    r.client = client
    r._last_kline_t = None
    return r


class TestMultiSymbolClose(unittest.TestCase):
    def test_first_call_only_seeds_baselines(self):
        c = _CandleClient({"BTC_USDT": 1000, "ETH_USDT": 1000})
        r = _runner(c, ["BTC_USDT", "ETH_USDT"])
        self.assertFalse(r._kline_closed(), "首次只记基线，不算收盘")

    def test_second_symbol_close_wakes(self):
        """第二个币收盘必须唤醒（旧判据下这是恒 False 的那条路径）。"""
        c = _CandleClient({"BTC_USDT": 1000, "ETH_USDT": 1000})
        r = _runner(c, ["BTC_USDT", "ETH_USDT"])
        r._kline_closed()                     # 建基线
        c.ts["ETH_USDT"] = 1900               # 只有 ETH 收盘
        self.assertTrue(r._kline_closed(), "ETH 收盘没唤醒（kline_close 仍只看首币）")
        self.assertEqual(r._kline_close_trigger(), "kline_close:ETH_USDT",
                         "触发标签必须带出是哪个币收盘的")
        self.assertFalse(r._kline_closed(), "同一根 K 线不得重复唤醒")

    def test_first_symbol_close_still_wakes(self):
        c = _CandleClient({"BTC_USDT": 1000, "ETH_USDT": 1000})
        r = _runner(c, ["BTC_USDT", "ETH_USDT"])
        r._kline_closed()
        c.ts["BTC_USDT"] = 1900
        self.assertTrue(r._kline_closed())
        self.assertEqual(r._kline_close_trigger(), "kline_close:BTC_USDT")

    def test_same_minute_multi_close_wakes_once(self):
        c = _CandleClient({"BTC_USDT": 1000, "ETH_USDT": 1000, "SOL_USDT": 1000})
        r = _runner(c, ["BTC_USDT", "ETH_USDT", "SOL_USDT"])
        r._kline_closed()
        c.ts["ETH_USDT"] = 1900
        c.ts["SOL_USDT"] = 1900
        self.assertTrue(r._kline_closed(), "多币同时收盘应唤醒")
        self.assertFalse(r._kline_closed(), "同一分钟多币收盘只唤醒一次")
        label = r._kline_close_trigger()
        self.assertIn("ETH_USDT", label)
        self.assertIn("SOL_USDT", label)
        self.assertNotIn("BTC_USDT", label, "没收盘的币不该出现在触发标签里")

    def test_failing_symbol_does_not_block_others(self):
        """一个币取数失败 → 只跳过它自己：不唤醒、不影响其他币的基线。"""
        c = _CandleClient({"BTC_USDT": None, "ETH_USDT": 1000})
        r = _runner(c, ["BTC_USDT", "ETH_USDT"])
        self.assertFalse(r._kline_closed(), "取数失败不算收盘")
        c.ts["ETH_USDT"] = 1900
        self.assertTrue(r._kline_closed(), "首币失败不该拖住第二个币")
        # 失败的币恢复后，仍按自己的基线判收盘（基线没被失败那轮写坏）
        c.ts["BTC_USDT"] = 1900
        self.assertTrue(r._kline_closed(), "BTC 恢复后自己的收盘事件仍要唤醒")

    def test_empty_universe_never_wakes(self):
        c = _CandleClient({"BTC_USDT": 1000})
        r = _runner(c, [])
        self.assertFalse(r._kline_closed())
        self.assertEqual(r._kline_close_trigger(), "kline_close")


class TestSingleSymbolUnchanged(unittest.TestCase):
    """I11：单币路径逐字不变（标签、基线语义都与改动前一致）。"""

    def test_trigger_label_is_plain_kline_close(self):
        c = _CandleClient({"BTC_USDT": 1000})
        r = _runner(c, ["BTC_USDT"])
        r._kline_closed()
        c.ts["BTC_USDT"] = 1900
        self.assertTrue(r._kline_closed())
        self.assertEqual(r._kline_close_trigger(), "kline_close",
                         "单币标签必须与改动前逐字一致（plan[kline_close]）")

    def test_last_kline_t_stays_int_and_writable(self):
        """历史属性名 `_last_kline_t` 仍按 int 读写（既有测试/调用方依赖）。"""
        c = _CandleClient({"BTC_USDT": 1000})
        r = _runner(c, ["BTC_USDT"])
        r._kline_closed()
        self.assertIsInstance(r._last_kline_t, int)
        r._last_kline_t = r._last_kline_t - 900
        self.assertTrue(r._kline_closed(), "把基线往回拨 900 应算新收盘")


class TestLegacyJudgementWouldMiss(unittest.TestCase):
    """证明用例真能抓住 bug：把旧判据（只看 `symbols[0]`）装回去必须精确失败。"""

    def test_legacy_first_symbol_judgement_would_miss(self):
        from omnialpha.strategist.market import fetch_rest_candles

        def legacy_first_symbol_only(self) -> bool:
            sym = self.cfg.symbols[0]
            rows = fetch_rest_candles(self.client, sym, "15m", 2)
            if not rows:
                return False
            ts = int((rows[-1] or {}).get("t") or 0)
            closed = self._last_kline_t is not None and ts > self._last_kline_t
            self._last_kline_t = ts
            return closed

        c = _CandleClient({"BTC_USDT": 1000, "ETH_USDT": 1000})
        r = _runner(c, ["BTC_USDT", "ETH_USDT"])
        r._kline_closed()
        c.ts["ETH_USDT"] = 1900
        with mock.patch.object(PlanRunner, "_kline_closed", legacy_first_symbol_only):
            self.assertFalse(r._kline_closed(),
                             "旧判据只看首币 —— 这条用例在回退实现时会失败")

    def test_source_does_not_hardcode_first_symbol(self):
        """源码级反断言：`_kline_closed` 不得再直接取 `symbols[0]`。"""
        import inspect

        src = inspect.getsource(PlanRunner._kline_closed)
        self.assertNotIn("symbols[0]", src, "kline_close 又只看首币了")
        loop_src = inspect.getsource(PlanRunner.run_forever)
        self.assertIn("_kline_close_trigger()", loop_src,
                      "run_forever 必须用带 symbol 的触发标签")


if __name__ == "__main__":
    unittest.main()
