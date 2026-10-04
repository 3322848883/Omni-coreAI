# -*- coding: utf-8 -*-
"""tv_vol_oi_footprint 与 Leviathan 原版 Pine 语义的对齐测试。

锁定六类原版语义：

  1. `get_vol` 的重叠语义与 `nz()` 兜底（无重叠 → 0，`height` 为 0 → 0）
  2. 档位自 `profHigh` **向下**排（最高档在前）
  3. `profHigh`/`profLow` 排除最后一根 bar（原版 `ta.highest(...)[1]`）
  4. 实体/影线权重：实体 1、上下影线各 2
  5. **影线的量一半记绿一半记红**；实体量才按阴阳归属
  6. OI 模式只用实体、不用影线，且 `total` 只计增仓（`vpTotal = vpGreen`）

用例用手算可验证的小数据，并校验成交量守恒（Volume 模式下 green+red 应等于
落入区间的总量），守恒一旦破就说明分摊公式写错了。
"""

import unittest

from omnialpha.strategist.tv_indicators.vol_oi_footprint import (
    MODE_OI,
    MODE_VOLUME,
    overlap_amount,
    vol_oi_footprint,
)


def bars(specs):
    """specs: [(o, h, l, c, v), ...] → (highs, lows, opens, closes, volumes)"""
    return ([s[1] for s in specs], [s[2] for s in specs],
            [s[0] for s in specs], [s[3] for s in specs], [s[4] for s in specs])


class TestOverlapAmount(unittest.TestCase):
    """原版 `get_vol`。"""

    def test_partial_overlap(self):
        # [0,10] ∩ [5,15] = 5，height=10，vol=100 → 5*100/10
        self.assertAlmostEqual(overlap_amount(0, 10, 5, 15, 10, 100), 50.0)

    def test_no_overlap_gives_zero(self):
        self.assertEqual(overlap_amount(0, 10, 20, 30, 10, 100), 0.0)

    def test_fully_contained(self):
        # [0,10] ∩ [2,8] = 6，height=6，vol=60 → 6*60/6
        self.assertAlmostEqual(overlap_amount(0, 10, 2, 8, 6, 60), 60.0)

    def test_zero_height_returns_zero(self):
        # height=0 → 原版 0/0=na，nz() 转 0
        self.assertEqual(overlap_amount(0, 10, 5, 15, 0, 100), 0.0)

    def test_argument_order_irrelevant(self):
        self.assertAlmostEqual(overlap_amount(10, 0, 15, 5, 10, 100),
                               overlap_amount(0, 10, 5, 15, 10, 100))


class TestLevelDirection(unittest.TestCase):
    def test_top_level_is_highest_price(self):
        # profHigh=110、profLow=100、resolution=2 → gap=5
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        self.assertEqual(r["profile"]["high"], 110.0)
        self.assertEqual(r["profile"]["low"], 100.0)
        self.assertEqual(r["profile"]["step"], 5.0)
        # 档位自高向低排
        self.assertEqual(r["levels"][0]["price_top"], 110.0)
        self.assertEqual(r["levels"][0]["price_bot"], 105.0)
        self.assertEqual(r["levels"][1]["price_top"], 105.0)
        self.assertEqual(r["levels"][1]["price_bot"], 100.0)

    def test_last_bar_excluded_from_range(self):
        # 最后一根冲到 200，但区间极值只看前面（原版 [1] 语义）
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (110, 200, 100, 110, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        self.assertEqual(r["profile"]["high"], 110.0)


class TestVolumeAllocation(unittest.TestCase):
    def test_body_splits_evenly_across_two_levels(self):
        # 实体 100→110（body=10）跨两档各一半 → 每档 50
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        self.assertAlmostEqual(r["levels"][0]["green"], 50.0)
        self.assertAlmostEqual(r["levels"][1]["green"], 50.0)
        self.assertAlmostEqual(r["levels"][0]["red"], 0.0)
        self.assertAlmostEqual(r["totals"]["green"], 100.0)
        self.assertAlmostEqual(r["totals"]["delta"], 100.0)
        self.assertEqual(r["dominant"], "buyers")

    def test_wicks_split_half_green_half_red(self):
        # doji o=c=105、h=110、l=100：上下影线各 5，无实体
        # 影线量一半绿一半红 → 每档 green=red=25
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (105, 110, 100, 105, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        for k in (0, 1):
            self.assertAlmostEqual(r["levels"][k]["green"], 25.0)
            self.assertAlmostEqual(r["levels"][k]["red"], 25.0)
        self.assertAlmostEqual(r["totals"]["delta"], 0.0)

    def test_volume_conserved(self):
        # 分摊后 green+red 必须等于落入区间的总量（守恒一破即公式有误）
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (105, 110, 100, 105, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        self.assertAlmostEqual(r["totals"]["green"] + r["totals"]["red"], 100.0)

    def test_volume_conserved_with_wicks_and_body(self):
        # o=102、c=108、h=112、l=98：实体 + 上下影线齐全。
        # 区间极值由前两根给出（112 / 98），故该 bar 完整落在区间内 → 严格守恒
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 112, 98, 112, 0),
                              (102, 112, 98, 108, 200)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=4)
        self.assertAlmostEqual(r["totals"]["green"] + r["totals"]["red"], 200.0, places=6)

    def test_volume_outside_range_is_dropped(self):
        # bar 低点低于区间下界 → 超出部分不计入。这是原版行为（区间外无档位可放），
        # 不是 bug；记录下来以免日后被当成「量丢失」而误改分摊公式
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (102, 110, 90, 108, 200)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=4)
        self.assertLess(r["totals"]["green"] + r["totals"]["red"], 200.0)

    def test_bear_bar_body_goes_red(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (110, 110, 100, 100, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        self.assertAlmostEqual(r["totals"]["red"], 100.0)
        self.assertAlmostEqual(r["totals"]["green"], 0.0)
        self.assertEqual(r["dominant"], "sellers")


class TestOiMode(unittest.TestCase):
    """OI 模式看的是 ΔOI（不是 volume），故 oi 序列需让中间那根 ΔOI 为 0，
    才能把量集中到最后一根上。"""

    def test_oi_uses_body_only_and_total_is_green(self):
        # 仅最后一根 ΔOI=+100 的阳线：green 得量、red 为 0，total 只计 green
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 0)])
        oi = [1000.0, 1000.0, 1100.0]
        r = vol_oi_footprint(h, l, o, c, v, resolution=2, mode=MODE_OI, oi_values=oi)
        self.assertAlmostEqual(r["levels"][0]["green"], 50.0)
        self.assertAlmostEqual(r["levels"][1]["green"], 50.0)
        self.assertAlmostEqual(r["totals"]["green"], 100.0)
        self.assertAlmostEqual(r["totals"]["red"], 0.0)
        # 原版 OI 模式 vpTotal = vpGreen
        self.assertAlmostEqual(r["totals"]["total"], 100.0)

    def test_negative_delta_oi_goes_red_and_total_stays_green(self):
        # ΔOI=-100：量记 red，但 total 仍只计 green（原版语义）→ total 为 0
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 0)])
        oi = [1000.0, 1000.0, 900.0]
        r = vol_oi_footprint(h, l, o, c, v, resolution=2, mode=MODE_OI, oi_values=oi)
        self.assertAlmostEqual(r["totals"]["red"], 100.0)
        self.assertAlmostEqual(r["totals"]["green"], 0.0)
        self.assertAlmostEqual(r["totals"]["total"], 0.0)

    def test_oi_ignores_wicks(self):
        # 带影线的 bar：OI 模式只用实体，影线不贡献
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (105, 110, 100, 105, 0)])
        oi = [1000.0, 1000.0, 1100.0]        # ΔOI=+100，但该 bar body=0
        r = vol_oi_footprint(h, l, o, c, v, resolution=2, mode=MODE_OI, oi_values=oi)
        self.assertAlmostEqual(r["totals"]["green"], 0.0)
        self.assertAlmostEqual(r["totals"]["red"], 0.0)

    def test_oi_zero_delta_skipped(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 0)])
        oi = [1000.0, 1000.0, 1000.0]        # ΔOI 全 0
        r = vol_oi_footprint(h, l, o, c, v, resolution=2, mode=MODE_OI, oi_values=oi)
        self.assertAlmostEqual(r["totals"]["green"], 0.0)
        self.assertAlmostEqual(r["totals"]["red"], 0.0)


class TestPocAndSignals(unittest.TestCase):
    def test_poc_is_max_total_level(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 0)])
        oi = [1000.0, 1000.0, 1300.0]        # ΔOI=+300 集中在最后一根
        r = vol_oi_footprint(h, l, o, c, v, resolution=2, mode=MODE_OI, oi_values=oi)
        # 实体 10 跨两档各一半 → 每档 150，并列 → 取第一个
        self.assertEqual(r["poc"]["level"], 0)
        self.assertAlmostEqual(r["poc"]["total"], 150.0)

    def test_positive_delta_levels(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        # 阳线实体 → 两档 delta 均为正
        self.assertEqual(len(r["positive_delta_levels"]), 2)
        self.assertAlmostEqual(r["positive_delta_levels"][0], 107.5)


class TestGuards(unittest.TestCase):
    def test_too_few_bars(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0), (100, 110, 100, 110, 0)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        self.assertIn("error", r)

    def test_oi_mode_requires_oi_values(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2, mode=MODE_OI)
        self.assertIn("error", r)

    def test_oi_values_length_mismatch(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2, mode=MODE_OI, oi_values=[1.0])
        self.assertIn("error", r)

    def test_unknown_mode(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0),
                              (100, 110, 100, 110, 0),
                              (100, 110, 100, 110, 100)])
        r = vol_oi_footprint(h, l, o, c, v, resolution=2, mode="nope")
        self.assertIn("error", r)

    def test_flat_range(self):
        h, l, o, c, v = bars([(100, 100, 100, 100, 0)] * 3)
        r = vol_oi_footprint(h, l, o, c, v, resolution=2)
        self.assertIn("error", r)

    def test_levels_length_matches_resolution(self):
        specs = [(100 + i, 101 + i, 99 + i, 100 + i, 10.0) for i in range(30)]
        h, l, o, c, v = bars(specs)
        r = vol_oi_footprint(h, l, o, c, v, resolution=25)
        self.assertEqual(len(r["levels"]), 25)
        self.assertEqual(r["profile"]["resolution"], 25)


if __name__ == "__main__":
    unittest.main()
