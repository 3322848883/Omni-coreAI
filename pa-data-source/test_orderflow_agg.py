# -*- coding: utf-8 -*-
"""orderflow_agg 单元测试。

重点在**撤单 / 成交的归因**：增量只给挂单量的差，不给原因。若把撤单误算成
成交，成交量会虚增，而「成交密度」「大单」这些下游指标会一起失真且难以察觉。

其余覆盖：序列号校验、快照不重置存活时间、长寿挂单结算、盘口分级、逐笔聚合。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orderflow_agg import (  # noqa: E402
    GRADE_TABLE,
    WALL_MIN_AGE_SEC,
    BookLevel,
    OrderBookState,
    UpdateResult,
    apply_snapshot,
    apply_update,
    attribute_decrease,
    book_metrics,
    footprint_bucket,
    grade,
    relative_spread,
    sequence_ok,
    tape_bucket,
)


class TestAttributeDecrease(unittest.TestCase):
    """撤单 / 成交归因 —— 本模块最关键的函数。"""

    def test_all_eaten(self):
        # 减少 100、同价位成交 100 → 全是被吃
        self.assertEqual(attribute_decrease(100.0, 0.0, 100.0), (100.0, 0.0))

    def test_partial_cancel(self):
        # 减少 100、成交仅 30 → 30 被吃，70 是撤单
        self.assertEqual(attribute_decrease(100.0, 0.0, 30.0), (30.0, 70.0))

    def test_all_cancelled(self):
        # 减少 100、无成交 → 全是撤单
        self.assertEqual(attribute_decrease(100.0, 0.0, 0.0), (0.0, 100.0))

    def test_traded_exceeds_decrease(self):
        # 成交多于减少（该档被吃的同时还有新挂单补进来）→ 减少量全算被吃
        eaten, cancelled = attribute_decrease(100.0, 0.0, 250.0)
        self.assertEqual(eaten, 100.0)
        self.assertEqual(cancelled, 0.0)

    def test_partial_decrease(self):
        # 只减少一部分：100 → 40，成交 20 → 20 被吃，40 撤单
        self.assertEqual(attribute_decrease(100.0, 40.0, 20.0), (20.0, 40.0))

    def test_size_increase_is_not_a_decrease(self):
        self.assertEqual(attribute_decrease(50.0, 80.0, 10.0), (0.0, 0.0))


class TestSequence(unittest.TestCase):
    def test_continuous_ok(self):
        st = OrderBookState(last_u=100)
        self.assertTrue(sequence_ok(st, 101))

    def test_gap_detected(self):
        st = OrderBookState(last_u=100)
        self.assertFalse(sequence_ok(st, 150))

    def test_no_history_ok(self):
        self.assertTrue(sequence_ok(OrderBookState(), 5))


class TestSnapshot(unittest.TestCase):
    def test_snapshot_builds_book(self):
        st = OrderBookState()
        apply_snapshot(st, [{"p": "100.0", "s": 10}, {"p": "99.0", "s": 5}],
                       [{"p": "101.0", "s": 7}], ts=1000.0, seq=42)
        self.assertEqual(st.last_u, 42)
        self.assertAlmostEqual(st.depth(5, True), 15.0)
        self.assertAlmostEqual(st.depth(5, False), 7.0)

    def test_snapshot_keeps_born_ts(self):
        # 快照刷新不应重置存活时间，否则长寿挂单永远统计不出来
        st = OrderBookState()
        apply_snapshot(st, [{"p": "100.0", "s": 10}], [], ts=1000.0)
        apply_snapshot(st, [{"p": "100.0", "s": 12}], [], ts=1030.0)
        self.assertAlmostEqual(st.bids[100.0].born_ts, 1000.0)
        self.assertAlmostEqual(st.bids[100.0].peak_size, 12.0)

    def test_zero_size_skipped(self):
        st = OrderBookState()
        apply_snapshot(st, [{"p": "100.0", "s": 0}], [], ts=1.0)
        self.assertEqual(len(st.bids), 0)


class TestUpdate(unittest.TestCase):
    def test_new_level_places(self):
        st = OrderBookState()
        res = apply_update(st, [{"p": "100.0", "s": 50}], True, ts=10.0)
        self.assertAlmostEqual(res.place_vol, 50.0)
        self.assertAlmostEqual(st.bids[100.0].size, 50.0)

    def test_increase_counts_as_place(self):
        st = OrderBookState()
        apply_update(st, [{"p": "100.0", "s": 50}], True, ts=10.0)
        res = apply_update(st, [{"p": "100.0", "s": 80}], True, ts=11.0)
        self.assertAlmostEqual(res.place_vol, 30.0)

    def test_decrease_attributed_by_traded_volume(self):
        st = OrderBookState()
        apply_update(st, [{"p": "100.0", "s": 100}], True, ts=10.0)
        # 减少到 40，同价位成交 20 → 20 被吃、40 撤单
        res = apply_update(st, [{"p": "100.0", "s": 40}], True, ts=11.0,
                           traded_by_price={100.0: 20.0})
        self.assertAlmostEqual(res.eaten_vol, 20.0)
        self.assertAlmostEqual(res.cancel_vol, 40.0)

    def test_level_removed_is_attributed(self):
        st = OrderBookState()
        apply_update(st, [{"p": "100.0", "s": 100}], True, ts=10.0)
        res = apply_update(st, [{"p": "100.0", "s": 0}], True, ts=11.0,
                           traded_by_price={100.0: 30.0})
        self.assertAlmostEqual(res.eaten_vol, 30.0)
        self.assertAlmostEqual(res.cancel_vol, 70.0)
        self.assertNotIn(100.0, st.bids)

    def test_long_lived_wall_recorded_on_removal(self):
        st = OrderBookState()
        apply_update(st, [{"p": "100.0", "s": 100}], True, ts=0.0)
        res = apply_update(st, [{"p": "100.0", "s": 0}], True,
                           ts=WALL_MIN_AGE_SEC + 5.0, traded_by_price={100.0: 100.0})
        self.assertEqual(len(res.closed_walls), 1)
        w = res.closed_walls[0]
        self.assertEqual(w["side"], "bid")
        self.assertEqual(w["outcome"], "eaten")
        self.assertGreaterEqual(w["age_sec"], WALL_MIN_AGE_SEC)

    def test_short_lived_level_not_recorded(self):
        st = OrderBookState()
        apply_update(st, [{"p": "100.0", "s": 100}], True, ts=0.0)
        res = apply_update(st, [{"p": "100.0", "s": 0}], True, ts=2.0,
                           traded_by_price={100.0: 0.0})
        self.assertEqual(len(res.closed_walls), 0)

    def test_ask_side_label(self):
        st = OrderBookState()
        apply_update(st, [{"p": "101.0", "s": 100}], False, ts=0.0)
        res = apply_update(st, [{"p": "101.0", "s": 0}], False,
                           ts=WALL_MIN_AGE_SEC + 1, traded_by_price={})
        self.assertEqual(res.closed_walls[0]["side"], "ask")
        self.assertEqual(res.closed_walls[0]["outcome"], "cancelled")


class TestSpreadAndGrade(unittest.TestCase):
    def test_relative_spread(self):
        st = OrderBookState()
        apply_snapshot(st, [{"p": "100.0", "s": 1}], [{"p": "100.1", "s": 1}], ts=0.0)
        # (100.1-100)/100.05
        self.assertAlmostEqual(relative_spread(st), 0.1 / 100.05, places=9)

    def test_no_book_returns_none(self):
        self.assertIsNone(relative_spread(OrderBookState()))

    def test_grade_excellent(self):
        self.assertEqual(grade(0.0001, 0.9), "excellent")

    def test_grade_normal(self):
        self.assertEqual(grade(0.0003, 0.6), "normal")

    def test_grade_poor_by_depth(self):
        # 价差够好但深度不足 → 降到 poor
        self.assertEqual(grade(0.0003, 0.4), "poor")

    def test_grade_bad_by_spread(self):
        self.assertEqual(grade(0.002, 0.9), "bad")

    def test_grade_unknown_without_spread(self):
        self.assertEqual(grade(None, 0.9), "unknown")

    def test_grade_table_ordered(self):
        # 阈值必须严格递增，否则分级会错乱
        limits = [sp for _, sp, _ in GRADE_TABLE]
        self.assertEqual(limits, sorted(limits))


class TestBookMetrics(unittest.TestCase):
    def _state(self):
        st = OrderBookState()
        apply_snapshot(st, [{"p": "100.0", "s": 10}, {"p": "99.9", "s": 10}],
                       [{"p": "100.1", "s": 10}], ts=0.0)
        return st

    def test_metrics_keys(self):
        m = book_metrics(self._state(), depth_hist_mean=30.0)
        for k in ("spread_pct", "depth_bid", "depth_ask", "depth_ratio",
                  "place_vol", "cancel_vol", "cancel_rate", "traded_vol",
                  "intensity", "grade", "levels"):
            self.assertIn(k, m)

    def test_depth_ratio_and_cancel_rate(self):
        st = self._state()
        res = UpdateResult(place_vol=100.0, cancel_vol=15.0)
        m = book_metrics(st, depth_hist_mean=30.0, res=res,
                         prev_total=100.0, traded_vol=50.0, window_sec=5.0)
        self.assertAlmostEqual(m["depth_ratio"], 1.0, places=4)
        self.assertAlmostEqual(m["cancel_rate"], 0.15, places=4)
        self.assertAlmostEqual(m["intensity"], 10.0, places=4)

    def test_grade_in_metrics(self):
        st = OrderBookState()
        apply_snapshot(st, [{"p": "100.00", "s": 100}], [{"p": "100.01", "s": 100}], ts=0.0)
        m = book_metrics(st, depth_hist_mean=100.0)
        self.assertIn(m["grade"], ("excellent", "normal", "poor", "bad"))


class TestTapeAndFootprint(unittest.TestCase):
    def test_tape_direction_by_sign(self):
        r = tape_bucket([{"price": "100", "size": 60},
                         {"price": "100", "size": -40}], big_threshold=1000)
        self.assertAlmostEqual(r["buy_size"], 60.0)
        self.assertAlmostEqual(r["sell_size"], 40.0)
        self.assertAlmostEqual(r["delta"], 20.0)

    def test_tape_big_trade_counting(self):
        r = tape_bucket([{"price": "100", "size": 5000},
                         {"price": "100", "size": -2000},
                         {"price": "100", "size": 10}], big_threshold=1000)
        self.assertEqual(r["big_count"], 2)
        self.assertAlmostEqual(r["big_size"], 7000.0)
        self.assertAlmostEqual(r["max_trade"], 5000.0)

    def test_tape_handles_bad_rows(self):
        r = tape_bucket([{"price": "100", "size": "abc"}, {}], big_threshold=1)
        self.assertEqual(r["big_count"], 0)
        self.assertEqual(r["buy_size"], 0.0)

    def test_footprint_tick_alignment(self):
        fp = footprint_bucket([{"price": "100.02", "size": 10},
                               {"price": "100.04", "size": -5}], tick=0.1)
        # 两个价都落到 100.0
        self.assertIn(100.0, fp)
        self.assertAlmostEqual(fp[100.0][0], 10.0)
        self.assertAlmostEqual(fp[100.0][1], 5.0)

    def test_footprint_separates_levels(self):
        fp = footprint_bucket([{"price": "100.0", "size": 10},
                               {"price": "100.2", "size": 3}], tick=0.1)
        self.assertEqual(len(fp), 2)

    def test_footprint_zero_tick(self):
        self.assertEqual(footprint_bucket([{"price": "100", "size": 1}], tick=0), {})


class TestBookLevel(unittest.TestCase):
    def test_age_never_negative(self):
        lv = BookLevel(size=1.0, born_ts=100.0)
        self.assertEqual(lv.age(90.0), 0.0)


if __name__ == "__main__":
    unittest.main()
