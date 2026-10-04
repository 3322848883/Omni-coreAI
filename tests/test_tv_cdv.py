# -*- coding: utf-8 -*-
"""tv_cdv 与 LonesomeTheBlue 原版 Pine 语义的对齐测试。

锁定五类原版语义：

  1. `_rate` 的取值域：阳线 ∈ [0.5, 1]、阴线 ∈ [0, 0.5]，由实体占整根的比例决定
  2. **`nz(ret) == 0 ? 0.5 : ret` 把 ret 恰好为 0 也替换成 0.5** ——
     于是「上下影线均为 0 的纯实体阴线」rate 是 0.5 而非 0（原版行为，不是 bug）
  3. `tw+bw+body == 0`（完全无波动）→ 0.5
  4. delta 符号由 `close >= open` 决定（平盘算正）
  5. CDV 蜡烛的 OHLC：`o = cumdelta[1]`、`h/l = 相邻两根 CDV 的极值`

第 2 条最容易被「顺手修正」成 0，那样纯实体阴线的 delta 会从 -0.5v 变成 -v，
强度翻倍。测试专门把它钉住。
"""

import unittest

from omnialpha.strategist.tv_indicators.cdv import (
    cdv_rate,
    cumulative_delta_volume,
    heikin_ashi_from,
)


class TestRate(unittest.TestCase):
    def test_full_body_bull_is_one(self):
        # 阳线、上下影线为 0 → 0.5*(0+0+2*body)/body = 1.0
        self.assertAlmostEqual(cdv_rate(110.0, 100.0, 100.0, 110.0, True), 1.0)

    def test_full_body_bear_rate_false_falls_back_to_half(self):
        # cond=False 支：纯实体阴线 → 原始 ret = 0 → 被 nz(ret)==0 替换为 0.5。
        # 这支在原版里从不被读取（delta 两支都取 cond=True），此处只钉住函数行为。
        self.assertAlmostEqual(cdv_rate(110.0, 100.0, 110.0, 100.0, False), 0.5)

    def test_full_body_bear_rate_true_is_one(self):
        # 真正生效的那支：cond=True → 0.5*(0+0+2*body)/body = 1.0
        self.assertAlmostEqual(cdv_rate(110.0, 100.0, 110.0, 100.0, True), 1.0)

    def test_doji_is_half(self):
        # body = 0、tw = bw = 10 → 0.5*(20)/(20) = 0.5（无论 cond）
        self.assertAlmostEqual(cdv_rate(110.0, 90.0, 100.0, 100.0, True), 0.5)
        self.assertAlmostEqual(cdv_rate(110.0, 90.0, 100.0, 100.0, False), 0.5)

    def test_flat_bar_is_half(self):
        # h == l == o == c → 分母为 0 → 0.5
        self.assertAlmostEqual(cdv_rate(100.0, 100.0, 100.0, 100.0, True), 0.5)
        self.assertAlmostEqual(cdv_rate(100.0, 100.0, 100.0, 100.0, False), 0.5)

    def test_bull_rate_within_bounds(self):
        # 半影线：tw=5、bw=5、body=10 → 0.5*(10+20)/20 = 0.75
        self.assertAlmostEqual(cdv_rate(115.0, 95.0, 100.0, 110.0, True), 0.75)

    def test_bear_rate_within_bounds(self):
        # 阴线、半影线：tw=5、bw=5、body=10 → 0.5*(10+0)/20 = 0.25
        self.assertAlmostEqual(cdv_rate(115.0, 95.0, 110.0, 100.0, False), 0.25)


class TestDelta(unittest.TestCase):
    def _run(self, specs):
        o = [s[0] for s in specs]
        h = [s[1] for s in specs]
        l = [s[2] for s in specs]
        c = [s[3] for s in specs]
        v = [s[4] for s in specs]
        return cumulative_delta_volume(o, h, l, c, v)

    def test_bull_body_gives_positive_full_volume(self):
        # 纯实体阳线 rate=1 → delta = +volume
        r = self._run([(100.0, 110.0, 100.0, 110.0, 100.0)])
        self.assertAlmostEqual(r["delta"][0], 100.0)
        self.assertAlmostEqual(r["cdv"][0], 100.0)

    def test_bear_body_gives_full_negative(self):
        # 纯实体阴线：实际生效的是 rate(cond=True) = 1.0 → delta = -volume
        r = self._run([(110.0, 110.0, 100.0, 100.0, 100.0)])
        self.assertAlmostEqual(r["delta"][0], -100.0)

    def test_doji_counts_as_positive_half(self):
        # c == o（平盘）走 `close >= open` 分支 → 正；doji rate = 0.5
        r = self._run([(100.0, 110.0, 90.0, 100.0, 100.0)])
        self.assertGreater(r["delta"][0], 0.0)
        self.assertAlmostEqual(r["delta"][0], 50.0)

    def test_cdv_accumulates(self):
        # delta = [+100, -100, +100] → cdv = [100, 0, 100]
        r = self._run([
            (100.0, 110.0, 100.0, 110.0, 100.0),   # +100
            (110.0, 110.0, 100.0, 100.0, 100.0),   # -100
            (100.0, 110.0, 100.0, 110.0, 100.0),   # +100
        ])
        self.assertAlmostEqual(r["delta"][0], 100.0)
        self.assertAlmostEqual(r["delta"][1], -100.0)
        self.assertAlmostEqual(r["delta"][2], 100.0)
        self.assertAlmostEqual(r["cdv"][0], 100.0)
        self.assertAlmostEqual(r["cdv"][1], 0.0)
        self.assertAlmostEqual(r["cdv"][2], 100.0)

    def test_cdv_candles_use_previous_close(self):
        # 原版 o = cumdelta[1]、h/l = 两根 CDV 的极值
        r = self._run([
            (100.0, 110.0, 100.0, 110.0, 100.0),   # cdv 100
            (110.0, 110.0, 100.0, 100.0, 100.0),   # cdv 0
        ])
        self.assertIsNone(r["cdv_open"][0])
        self.assertAlmostEqual(r["cdv_open"][1], 100.0)     # 前一根 cdv
        self.assertAlmostEqual(r["cdv_high"][1], 100.0)     # max(0, 100)
        self.assertAlmostEqual(r["cdv_low"][1], 0.0)        # min(0, 100)


class TestHeikinAshi(unittest.TestCase):
    def test_first_bar_open_is_midpoint(self):
        r = cumulative_delta_volume(
            [100.0, 110.0], [110.0, 110.0], [100.0, 100.0], [110.0, 100.0], [100.0, 100.0])
        ha = heikin_ashi_from(r["cdv_open"], r["cdv_high"], r["cdv_low"], r["cdv_close"])
        # 首根 haopen = (o + c) / 2，但首根 o 为 None → 原版只在有值时算
        self.assertIsNone(ha["open"][0])
        # 第二根有完整 OHLC
        self.assertIsNotNone(ha["close"][1])
        self.assertAlmostEqual(ha["close"][1],
                               (r["cdv_open"][1] + r["cdv_high"][1]
                                + r["cdv_low"][1] + r["cdv_close"][1]) / 4.0)

    def test_ha_high_low_enclose(self):
        r = cumulative_delta_volume(
            [100.0, 110.0, 100.0], [110.0, 110.0, 110.0],
            [100.0, 100.0, 100.0], [110.0, 100.0, 110.0], [100.0, 100.0, 100.0])
        ha = heikin_ashi_from(r["cdv_open"], r["cdv_high"], r["cdv_low"], r["cdv_close"])
        for i in (1, 2):
            self.assertGreaterEqual(ha["high"][i], ha["open"][i])
            self.assertGreaterEqual(ha["high"][i], ha["close"][i])
            self.assertLessEqual(ha["low"][i], ha["open"][i])
            self.assertLessEqual(ha["low"][i], ha["close"][i])


class TestGuards(unittest.TestCase):
    def test_empty_input(self):
        r = cumulative_delta_volume([], [], [], [], [])
        self.assertEqual(r["cdv"], [])
        self.assertEqual(r["delta"], [])

    def test_single_bar(self):
        r = cumulative_delta_volume([100.0], [110.0], [100.0], [110.0], [100.0])
        self.assertEqual(len(r["cdv"]), 1)
        self.assertAlmostEqual(r["cdv"][0], 100.0)


if __name__ == "__main__":
    unittest.main()
