# -*- coding: utf-8 -*-
"""tv_delta_flow_profile 与 LuxAlgo 原版 Pine 语义的对齐测试。

锁定五类原版语义，避免移植时「顺手优化」导致漂移：

  1. `vPOR` 四种重叠情况——**判定顺序敏感**，换序会改变边界归属
  2. 档位是半开区间 `[pLL, pLL+pSTP)`——恰好等于最高价的 bar 不计入最高档
  3. money flow = `volume × vPOR × 档位中间价`，不是纯成交量
  4. Delta = `2×bull_flow − total_flow`（等价于买 − 卖）
  5. PoC 取**第一个**最大值（对齐 `array.indexof`）；Developing PoC 逐 bar 记录

用例尽量用手算可验证的小数据，且优先整数运算，避免浮点误差掩盖逻辑错误。
"""

import unittest

from omnialpha.strategist.tv_indicators.delta_flow_profile import (
    POLARITY_BAR,
    POLARITY_PRESSURE,
    bull_flags,
    delta_flow_profile,
    overlap_ratio,
)


class TestOverlapRatio(unittest.TestCase):
    """vPOR：bar 落在档位内的比例（档位 [100, 110)，step=10）。"""

    def test_lower_inside_upper_beyond(self):
        # bar [105,120]：下端在档内、上端超出 → (110-105)/(120-105)
        self.assertAlmostEqual(overlap_ratio(105, 120, 100, 10), 5.0 / 15.0)

    def test_upper_inside_lower_beyond(self):
        # bar [95,108]：上端在档内、下端超出 → (108-100)/(108-95)
        self.assertAlmostEqual(overlap_ratio(95, 108, 100, 10), 8.0 / 13.0)

    def test_fully_inside(self):
        # bar [102,106] 完全落在档内 → 1
        self.assertEqual(overlap_ratio(102, 106, 100, 10), 1.0)

    def test_fully_covering(self):
        # bar [90,130] 完全覆盖档位 → 10/(130-90)
        self.assertAlmostEqual(overlap_ratio(90, 130, 100, 10), 10.0 / 40.0)

    def test_flat_bar_converges_to_one(self):
        # h == l：原版算出 na（0/0），此处收敛为 1.0——该 bar 必然整根落在某档内
        self.assertEqual(overlap_ratio(105, 105, 100, 10), 1.0)

    def test_order_matters_at_lower_boundary(self):
        # bar 起点正好等于档位下界：应走「下端在档内」分支，而非「完全覆盖」
        # [100,115] 覆盖档 [100,110) → (110-100)/(115-100)
        self.assertAlmostEqual(overlap_ratio(100, 115, 100, 10), 10.0 / 15.0)


class TestPolarity(unittest.TestCase):
    """两种极性判定方式。"""

    def test_bar_polarity(self):
        f = bull_flags([100.0, 100.0], [110.0, 110.0], [90.0, 90.0],
                       [105.0, 95.0], POLARITY_BAR)
        self.assertEqual(f, [True, False])

    def test_bar_pressure(self):
        # bar0: (105-90)=15 > (110-105)=5 → True
        # bar1: (95-90)=5  > (110-95)=15 → False
        f = bull_flags([100.0, 100.0], [110.0, 110.0], [90.0, 90.0],
                       [105.0, 95.0], POLARITY_PRESSURE)
        self.assertEqual(f, [True, False])

    def test_pressure_differs_from_polarity(self):
        # 阳线（c>o）但收盘贴近低点 → polarity 判多、pressure 判空
        o, h, l, c = [90.0], [110.0], [89.0], [91.0]
        self.assertTrue(bull_flags(o, h, l, c, POLARITY_BAR)[0])
        self.assertFalse(bull_flags(o, h, l, c, POLARITY_PRESSURE)[0])


class TestMoneyFlowMath(unittest.TestCase):
    """money flow = volume × vPOR × 档位中间价。"""

    def test_single_contributing_bar(self):
        # 3 根 bar，只有最后一根有量；区间 [100,110] 分 2 档（step=5）
        #   档0 [100,105) 中间价 102.5 → vPOR 0.5 → 100*0.5*102.5 = 5125
        #   档1 [105,110) 中间价 107.5 → vPOR 0.5 → 100*0.5*107.5 = 5375
        o = [100.0, 110.0, 105.0]
        h = [100.0, 110.0, 110.0]
        l = [100.0, 110.0, 100.0]
        c = [100.0, 110.0, 105.0]
        v = [0.0, 0.0, 100.0]
        r = delta_flow_profile(o, h, l, c, v, lookback=2, rows=2)

        self.assertEqual(r["profile"]["low"], 100.0)
        self.assertEqual(r["profile"]["high"], 110.0)
        self.assertEqual(r["profile"]["step"], 5.0)
        self.assertAlmostEqual(r["levels"][0]["price"], 102.5)
        self.assertAlmostEqual(r["levels"][1]["price"], 107.5)
        self.assertAlmostEqual(r["total_money_flow"], 10500.0)
        self.assertEqual(r["poc"]["level"], 1)
        self.assertAlmostEqual(r["poc"]["price"], 107.5)

    def test_money_flow_is_price_weighted_not_volume(self):
        # 同样 100 的量落在中间价 105 的档位 → money flow = 100×1×105 = 10500
        # （若误用纯成交量则为 100，差两个数量级，便于发现移植错误）
        o = [100.0, 100.0, 100.0]
        h = [100.0, 110.0, 100.0]
        l = [100.0, 100.0, 100.0]
        c = [100.0, 100.0, 100.0]
        v = [0.0, 0.0, 100.0]
        r = delta_flow_profile(o, h, l, c, v, lookback=2, rows=1)
        self.assertEqual(r["profile"]["step"], 10.0)
        self.assertAlmostEqual(r["levels"][0]["price"], 105.0)
        self.assertAlmostEqual(r["total_money_flow"], 10500.0)

    def test_volume_zero_gives_zero_flow(self):
        o = [100.0, 105.0, 110.0]
        h = [100.0, 105.0, 110.0]
        l = [100.0, 105.0, 110.0]
        c = [100.0, 105.0, 110.0]
        v = [0.0, 0.0, 0.0]
        r = delta_flow_profile(o, h, l, c, v, lookback=2, rows=3)
        self.assertEqual(r["total_money_flow"], 0.0)
        self.assertEqual(r["poc"]["level"], 0)


class TestDelta(unittest.TestCase):
    """Delta = 2×bull_flow − total_flow。"""

    def _run(self, last_close, polarity=POLARITY_BAR):
        o = [100.0, 100.0, 100.0]
        h = [105.0, 105.0, 105.0]
        l = [100.0, 100.0, 100.0]
        c = [100.0, 100.0, last_close]
        v = [0.0, 0.0, 100.0]
        return delta_flow_profile(o, h, l, c, v, lookback=2, rows=1, polarity=polarity)

    def test_bull_bar_makes_positive_delta(self):
        r = self._run(104.0)          # c > o → bull
        self.assertGreater(r["total_delta"], 0)
        self.assertEqual(r["delta_dominant"], "buyers")

    def test_bear_bar_makes_negative_delta(self):
        r = self._run(96.0)           # c < o → bear
        self.assertLess(r["total_delta"], 0)
        self.assertEqual(r["delta_dominant"], "sellers")

    def test_delta_equals_total_when_all_bull(self):
        # 全部量记入 bull 侧 → 2*X − X = X
        r = self._run(104.0)
        self.assertAlmostEqual(r["total_delta"], r["total_money_flow"])

    def test_doji_counts_as_bear(self):
        # c == o：原版用 `c > o`（严格大于）→ 不算 bull
        r = self._run(100.0)
        self.assertLessEqual(r["total_delta"], 0)
        self.assertEqual(r["delta_dominant"], "sellers")


class TestPoC(unittest.TestCase):
    """PoC 取第一个最大值；Developing PoC 记录迁移。"""

    def test_poc_takes_first_max(self):
        # 人为让档0/档1 money flow 精确相等（全整数运算）：
        #   档0 = 210*0.5*105 + 10*1*105 = 11025 + 1050 = 12075
        #   档1 = 210*0.5*115            = 12075
        # 原版 array.indexof 取第一个 → level 0
        o = [100.0, 100.0, 100.0]
        h = [100.0, 120.0, 100.0]
        l = [100.0, 100.0, 100.0]
        c = [100.0, 110.0, 100.0]
        v = [0.0, 210.0, 10.0]
        r = delta_flow_profile(o, h, l, c, v, lookback=2, rows=2)

        self.assertEqual(r["profile"]["step"], 10.0)
        self.assertAlmostEqual(r["poc"]["money_flow"], 12075.0)
        self.assertAlmostEqual(r["total_money_flow"], 24150.0)
        self.assertEqual(r["poc"]["level"], 0)

    def test_poc_share_pct(self):
        # 单档独占 → share 100%
        o = [100.0, 100.0, 100.0]
        h = [105.0, 105.0, 105.0]
        l = [100.0, 100.0, 100.0]
        c = [100.0, 100.0, 100.0]
        v = [0.0, 0.0, 100.0]
        r = delta_flow_profile(o, h, l, c, v, lookback=2, rows=1)
        self.assertAlmostEqual(r["poc"]["share_pct"], 100.0)

    def test_poc_path_migrates_up(self):
        # 前两根量在低位、后两根在高位 → 累积 PoC 最终上移
        o = [100.0, 100.0, 105.0, 105.0]
        h = [105.0, 105.0, 110.0, 110.0]
        l = [100.0, 100.0, 105.0, 105.0]
        c = [100.0, 100.0, 105.0, 105.0]
        v = [100.0, 100.0, 100.0, 100.0]
        r = delta_flow_profile(o, h, l, c, v, lookback=3, rows=2)

        self.assertEqual(r["profile"]["step"], 5.0)
        self.assertEqual(r["poc"]["level"], 1)
        self.assertEqual(r["poc_path"]["direction"], "up")
        self.assertEqual(r["poc_path"]["start_level"], 0)
        self.assertEqual(r["poc_path"]["end_level"], 1)
        self.assertGreaterEqual(r["poc_path"]["changes_count"], 2)

    def test_poc_path_flat_when_balanced(self):
        # 每根 bar 都对称覆盖两档 → 累积 PoC 始终停在同一档（高价档，因 money flow 乘了价格）
        o = [100.0, 100.0, 100.0]
        h = [110.0, 110.0, 110.0]
        l = [100.0, 100.0, 100.0]
        c = [100.0, 100.0, 100.0]
        v = [100.0, 100.0, 100.0]
        r = delta_flow_profile(o, h, l, c, v, lookback=2, rows=2)
        self.assertEqual(r["poc_path"]["changes_count"], 1)
        self.assertEqual(r["poc_path"]["direction"], "flat")


class TestGuards(unittest.TestCase):
    """边界与防御。"""

    def test_too_few_bars(self):
        r = delta_flow_profile([1.0, 2.0], [1.0, 2.0], [1.0, 2.0],
                               [1.0, 2.0], [1.0, 1.0])
        self.assertIn("error", r)

    def test_flat_price_range(self):
        r = delta_flow_profile([100.0] * 4, [100.0] * 4, [100.0] * 4,
                               [100.0] * 4, [1.0] * 4, lookback=3, rows=2)
        self.assertIn("error", r)

    def test_lookback_larger_than_bars(self):
        # lookback 超长时按 bars-1 截断（对齐原版 rpLN 的钳制）
        o = [100.0, 101.0, 102.0]
        h = [101.0, 102.0, 103.0]
        l = [99.0, 100.0, 101.0]
        c = [100.0, 101.0, 102.0]
        v = [10.0, 10.0, 10.0]
        r = delta_flow_profile(o, h, l, c, v, lookback=9999, rows=2)
        self.assertNotIn("error", r)
        self.assertEqual(r["profile"]["bars"], 3)

    def test_levels_length_matches_rows(self):
        o = [100.0 + i for i in range(30)]
        h = [101.0 + i for i in range(30)]
        l = [99.0 + i for i in range(30)]
        c = [100.0 + i for i in range(30)]
        v = [10.0] * 30
        r = delta_flow_profile(o, h, l, c, v, lookback=20, rows=25)
        self.assertEqual(len(r["levels"]), 25)
        self.assertEqual(r["profile"]["rows"], 25)


if __name__ == "__main__":
    unittest.main()
