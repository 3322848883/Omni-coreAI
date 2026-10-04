# -*- coding: utf-8 -*-
"""tv_oi_visible_range 与 Kioseff 原版 Pine 语义的对齐测试。

锁定六类原版语义：

  1. 档位是 `rows` 个等距**价格点**（含两端），不是区间
  2. `binary_search_leftmost` / `rightmost` 的落点语义
  3. ΔOI 按跨越档位数**均摊**（`div = |lx1-lx2|+1`）
  4. 四象限归属：价涨/跌 × ΔOI 增/减
  5. ΔOI == 0 或 `close == open` 的 bar 完全不计入
  6. Value Area 从 POC **对称**扩展；Summed 聚合区间**方向无关**

第 6 条的「方向无关」是历史缺陷点：`effSwitchReg` 里 `lx1 >= lx2`，
而 `calcDelta` 里 `lx1 <= lx2`（`rightmost(c1) <= leftmost(c2)`）——
两处方向相反，若写死 `range(lx2, lx1+1)`，Summed 会整段落空。
"""

import unittest

from omnialpha.strategist.tv_indicators.oi_visible_range import (
    _leftmost,
    _levels,
    _rightmost,
    _span,
    _value_area,
    oi_visible_range,
)


class TestLevels(unittest.TestCase):
    def test_evenly_spaced_points_inclusive(self):
        # 3 个点含两端：100 / 105 / 110
        self.assertEqual(_levels(100.0, 110.0, 3), [100.0, 105.0, 110.0])

    def test_rows_two_gives_endpoints(self):
        self.assertEqual(_levels(100.0, 110.0, 2), [100.0, 110.0])


class TestBinarySearch(unittest.TestCase):
    ARR = [0.0, 10.0, 20.0, 30.0, 40.0]

    def test_leftmost_exact(self):
        self.assertEqual(_leftmost(self.ARR, 20.0), 2)

    def test_leftmost_between(self):
        # 第一个 >= 25 的元素是 30（索引 3）
        self.assertEqual(_leftmost(self.ARR, 25.0), 3)

    def test_leftmost_at_max(self):
        self.assertEqual(_leftmost(self.ARR, 40.0), 4)

    def test_rightmost_exact(self):
        self.assertEqual(_rightmost(self.ARR, 20.0), 2)

    def test_rightmost_between(self):
        # 最后一个 <= 25 的元素是 20（索引 2）
        self.assertEqual(_rightmost(self.ARR, 25.0), 2)

    def test_rightmost_at_min(self):
        self.assertEqual(_rightmost(self.ARR, 0.0), 0)


class TestSpan(unittest.TestCase):
    """Pine `for a to b` 的方向无关性。"""

    def test_ascending(self):
        self.assertEqual(list(_span(0, 2)), [0, 1, 2])

    def test_descending(self):
        self.assertEqual(list(_span(2, 0)), [0, 1, 2])

    def test_single(self):
        self.assertEqual(list(_span(3, 3)), [3])


class TestQuadrants(unittest.TestCase):
    """四象限归属与均摊。"""

    def _bars(self):
        # min(low)=100、max(high)=110 → levels=[100,110]；每根 bar 跨 2 档（div=2）
        return dict(
            opens=[100.0, 100.0, 105.0, 108.0, 102.0],
            highs=[100.0, 110.0, 110.0, 110.0, 110.0],
            lows=[100.0, 100.0, 100.0, 100.0, 100.0],
            closes=[100.0, 105.0, 108.0, 102.0, 100.0],
            oi=[1000.0, 1100.0, 1050.0, 1150.0, 1100.0],
        )

    def test_quadrant_totals(self):
        b = self._bars()
        r = oi_visible_range(b["highs"], b["lows"], b["opens"], b["closes"], b["oi"], rows=2)
        q = r["quadrants"]
        # bar1 +100 涨 → buyers_entered   bar2 -50 涨 → sellers_exited
        # bar3 +100 跌 → sellers_entered  bar4 -50 跌 → buyers_exited
        self.assertAlmostEqual(q["buyers_entered"]["total"], 100.0)
        self.assertAlmostEqual(q["sellers_exited"]["total"], 50.0)
        self.assertAlmostEqual(q["sellers_entered"]["total"], 100.0)
        self.assertAlmostEqual(q["buyers_exited"]["total"], 50.0)
        self.assertAlmostEqual(r["total_abs_oi_change"], 300.0)

    def test_equal_split_across_levels(self):
        # 每根 bar 跨 2 档 → 量 50/50 均摊
        b = self._bars()
        r = oi_visible_range(b["highs"], b["lows"], b["opens"], b["closes"], b["oi"], rows=2)
        lv = r["levels"]
        self.assertEqual(len(lv), 2)
        self.assertAlmostEqual(lv[0]["buyers_entered"], 50.0)
        self.assertAlmostEqual(lv[1]["buyers_entered"], 50.0)

    def test_dominant_is_first_on_tie(self):
        b = self._bars()
        r = oi_visible_range(b["highs"], b["lows"], b["opens"], b["closes"], b["oi"], rows=2)
        # buyers_entered 与 sellers_entered 并列 100 → 取声明顺序靠前者
        self.assertEqual(r["dominant"], "buyers_entered")

    def test_share_pct_sums_to_100(self):
        b = self._bars()
        r = oi_visible_range(b["highs"], b["lows"], b["opens"], b["closes"], b["oi"], rows=2)
        total = sum(v["share_pct"] for v in r["quadrants"].values())
        self.assertAlmostEqual(total, 100.0, places=1)


class TestSkips(unittest.TestCase):
    """ΔOI == 0 与 close == open 的 bar 不计入。"""

    def test_zero_delta_oi_skipped(self):
        r = oi_visible_range(
            [110.0, 110.0, 110.0], [100.0, 100.0, 100.0],
            [100.0, 100.0, 100.0], [105.0, 105.0, 105.0],
            [1000.0, 1000.0, 1000.0], rows=2)
        self.assertEqual(r["total_abs_oi_change"], 0.0)

    def test_doji_skipped(self):
        # close == open（priceArr == 0）→ 即使 ΔOI 非 0 也不计入
        r = oi_visible_range(
            [110.0, 110.0, 110.0], [100.0, 100.0, 100.0],
            [100.0, 100.0, 100.0], [100.0, 100.0, 100.0],
            [1000.0, 1100.0, 1200.0], rows=2)
        self.assertEqual(r["total_abs_oi_change"], 0.0)


class TestValueArea(unittest.TestCase):
    def test_symmetric_expansion(self):
        # [10,20,40,20,10] pct=70：POC 在 2；i=1 → [1,4)=20+40+20=80 >=70 → (1,3)
        self.assertEqual(_value_area([10.0, 20.0, 40.0, 20.0, 10.0], 70.0), (1, 3))

    def test_expands_stepwise_until_threshold(self):
        # [5,5,20,20,20,20,5,5]：POC 在 2
        #   i=1 → [1,4)=5+20+20=45 <70；i=2 → [0,5)=5+5+20+20+20=70 → (0,4)
        self.assertEqual(
            _value_area([5.0, 5.0, 20.0, 20.0, 20.0, 20.0, 5.0, 5.0], 70.0), (0, 4))

    def test_stops_early_when_peak_dominates(self):
        # [10,50,30,5,5] pct=70：i=1 → [0,3)=90 >=70 → (0,2)
        self.assertEqual(_value_area([10.0, 50.0, 30.0, 5.0, 5.0], 70.0), (0, 2))

    def test_single_level_dominates(self):
        self.assertEqual(_value_area([0.0, 100.0, 0.0], 70.0), (0, 2))

    def test_all_zero_returns_none(self):
        self.assertEqual(_value_area([0.0, 0.0], 70.0), (None, None))

    def test_peak_at_left_edge_clamps(self):
        self.assertEqual(_value_area([100.0, 0.0, 0.0], 70.0), (0, 1))


class TestSummed(unittest.TestCase):
    def test_buckets_cover_levels_despite_reversed_span(self):
        # levels=[100,110]（rows=2），delta_rows=2 → 两个区间
        # calcDelta 里 lx1=rightmost(c1)=0、lx2=leftmost(c2)=1，即 lx1 < lx2：
        # 若 span 按 range(lx2, lx1+1) 写死会得到空区间、nonempty_buckets=0
        r = oi_visible_range(
            [110.0, 110.0, 110.0], [100.0, 100.0, 100.0],
            [100.0, 100.0, 100.0], [105.0, 105.0, 105.0],
            [1000.0, 1100.0, 1150.0], rows=2, delta_rows=2)
        s = r["summed"]
        self.assertEqual(s["rows"], 2)
        self.assertEqual(s["nonempty_buckets"], 2)
        self.assertEqual(len(s["top"]), 2)
        # 两个区间都覆盖全部两档 → 各 150
        self.assertAlmostEqual(s["top"][0]["total"], 150.0)

    def test_delta_rows_capped_at_125(self):
        # 原版非 merge 模式：deltaRows := min(deltaRows, 125)
        r = oi_visible_range(
            [110.0, 110.0, 110.0], [100.0, 100.0, 100.0],
            [100.0, 100.0, 100.0], [105.0, 105.0, 105.0],
            [1000.0, 1100.0, 1150.0], rows=2, delta_rows=500)
        self.assertEqual(r["summed"]["rows"], 125)


class TestGuards(unittest.TestCase):
    def test_too_few_bars(self):
        r = oi_visible_range([100.0, 101.0], [99.0, 100.0], [99.0, 100.0],
                             [100.0, 101.0], [1.0, 2.0])
        self.assertIn("error", r)

    def test_length_mismatch(self):
        r = oi_visible_range([100.0, 101.0, 102.0], [99.0, 100.0, 101.0],
                             [99.0, 100.0, 101.0], [100.0, 101.0, 102.0], [1.0, 2.0])
        self.assertIn("error", r)

    def test_flat_price(self):
        r = oi_visible_range([100.0] * 3, [100.0] * 3, [100.0] * 3,
                             [100.0] * 3, [1.0, 2.0, 3.0])
        self.assertIn("error", r)

    def test_levels_length_matches_rows(self):
        n = 30
        highs = [100.0 + i for i in range(n)]
        lows = [99.0 + i for i in range(n)]
        opens = [99.5 + i for i in range(n)]
        closes = [100.0 + i for i in range(n)]
        oi = [1000.0 + i * 10 for i in range(n)]
        r = oi_visible_range(highs, lows, opens, closes, oi, rows=20)
        self.assertEqual(len(r["levels"]), 20)
        self.assertEqual(r["profile"]["rows"], 20)


if __name__ == "__main__":
    unittest.main()
