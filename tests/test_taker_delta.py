# -*- coding: utf-8 -*-
"""taker_delta 测试：真 Delta / CVD 的计算与脏数据容错。

重点在「脏数据不打断累积」——`contract_stats` 偶尔会有缺字段的记录，
若把 None 当成 0 参与累积，CVD 会静默偏移且无法察觉。
"""

import unittest

from omnialpha.strategist.taker_delta import summarize, taker_delta_series


class TestTakerDeltaSeries(unittest.TestCase):
    def test_delta_and_cvd(self):
        stats = [
            {"time": 1, "long_taker_size": 100, "short_taker_size": 40},
            {"time": 2, "long_taker_size": 30, "short_taker_size": 80},
            {"time": 3, "long_taker_size": 50, "short_taker_size": 50},
        ]
        r = taker_delta_series(stats)
        self.assertEqual(r["delta"], [60.0, -50.0, 0.0])
        self.assertEqual(r["cvd"], [60.0, 10.0, 10.0])
        self.assertEqual(r["bars"], 3)

    def test_missing_field_does_not_shift_cvd(self):
        # 中间一条缺 taker 字段 → 该位置 None，累积保持不变
        stats = [
            {"time": 1, "long_taker_size": 100, "short_taker_size": 40},
            {"time": 2},
            {"time": 3, "long_taker_size": 30, "short_taker_size": 80},
        ]
        r = taker_delta_series(stats)
        self.assertEqual(r["delta"], [60.0, None, -50.0])
        self.assertEqual(r["cvd"], [60.0, None, 10.0])

    def test_string_numbers_accepted(self):
        # Gate 部分字段以字符串返回
        stats = [{"time": 1, "long_taker_size": "100", "short_taker_size": "40"}]
        r = taker_delta_series(stats)
        self.assertEqual(r["delta"], [60.0])

    def test_bad_value_treated_as_missing(self):
        stats = [{"time": 1, "long_taker_size": "abc", "short_taker_size": 40}]
        r = taker_delta_series(stats)
        self.assertEqual(r["delta"], [None])

    def test_empty(self):
        r = taker_delta_series([])
        self.assertEqual(r["bars"], 0)
        self.assertEqual(r["delta"], [])


class TestSummarize(unittest.TestCase):
    @staticmethod
    def _stats(n=3):
        return [
            {"time": i, "long_taker_size": 100 + i, "short_taker_size": 50,
             "lsr_taker": 2.0, "open_interest": 1000, "open_interest_usd": 100000,
             "lsr_account": 1.1, "top_lsr_account": 0.6, "top_lsr_size": 1.2,
             "top_long_size": 300, "top_short_size": 250,
             "long_users": 1000, "short_users": 900,
             "last_funding_rate": "0.0001",
             "long_liq_size": 10, "short_liq_size": 20}
            for i in range(1, n + 1)
        ]

    def test_keys(self):
        r = summarize(self._stats())
        for k in ("bars", "delta_last", "cvd_last", "delta_tail", "cvd_tail",
                  "taker", "positioning", "funding", "liquidation", "last_time"):
            self.assertIn(k, r)

    def test_values(self):
        r = summarize(self._stats(3))
        # delta = 51, 52, 53 → cvd = 156
        self.assertAlmostEqual(r["delta_last"], 53.0)
        self.assertAlmostEqual(r["cvd_last"], 156.0)
        self.assertAlmostEqual(r["taker"]["long_taker_size"], 103.0)
        self.assertAlmostEqual(r["positioning"]["top_lsr_size"], 1.2)
        self.assertAlmostEqual(r["positioning"]["top_long_size"], 300.0)
        self.assertAlmostEqual(r["funding"]["last_funding_rate"], 0.0001)
        self.assertEqual(r["last_time"], 3)

    def test_empty_returns_error(self):
        self.assertIn("error", summarize([]))

    def test_tail_length(self):
        r = summarize(self._stats(5), tail=2)
        self.assertEqual(len(r["delta_tail"]), 2)
        self.assertEqual(len(r["cvd_tail"]), 2)

    def test_skips_non_dict_rows(self):
        rows = self._stats(2) + ["junk", None]      # type: ignore[list-item]
        r = summarize(rows)                          # type: ignore[arg-type]
        self.assertEqual(r["bars"], 2)


if __name__ == "__main__":
    unittest.main()
