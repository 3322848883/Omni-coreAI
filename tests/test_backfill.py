# -*- coding: utf-8 -*-
"""深回填（pa-data-source/backfill_history.py）：to 游标分页 + 合成。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

from backfill_history import covered_span, fetch_paged, synthesize, to_bars  # noqa: E402

DAY = 86400


def fake_source(total_days: int, end_t: int = 1_800_000_000):
    """模拟 Gate：limit 上限 1000、按 to 向后翻页、每页会重叠 1 根。"""
    all_t = [end_t - i * DAY for i in range(total_days)][::-1]

    def fetch(interval, limit, to):
        rows = [t for t in all_t if to is None or t <= to]
        rows = rows[-min(limit, 1000):]
        return [{"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0, "sum": 0.0}
                for t in rows]
    return fetch


class TestFetchPaged(unittest.TestCase):
    def test_dedupe_and_target(self):
        fetch = fake_source(2500)
        rows = fetch_paged(fetch, "1d", target=1500, sleep_sec=0)
        ts = [r["t"] for r in rows]
        self.assertEqual(len(ts), 1500)
        self.assertEqual(len(ts), len(set(ts)), "分页重叠未去重")
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(ts, sorted(set(ts))[-1500:])

    def test_single_page_when_target_small(self):
        calls = []

        def fetch(interval, limit, to):
            calls.append((interval, limit, to))
            return [{"t": 1_800_000_000 - i * DAY, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0}
                    for i in range(10)]
        rows = fetch_paged(fetch, "1d", target=10, sleep_sec=0)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len(calls), 1)

    def test_stops_when_no_new_data(self):
        calls = []

        def fetch(interval, limit, to):
            calls.append(to)
            return [{"t": 1_800_000_000, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0}]
        rows = fetch_paged(fetch, "1d", target=100, sleep_sec=0, max_pages=5)
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(len(calls), 2)

    def test_limit_clamped_to_1000(self):
        limits = []

        def fetch(interval, limit, to):
            limits.append(limit)
            return []
        fetch_paged(fetch, "1d", target=5000, page=5000, sleep_sec=0)
        self.assertTrue(all(l <= 1000 for l in limits), limits)


class TestSynthesize(unittest.TestCase):
    def _hourly(self, n, start=1_800_000_000):
        return [{"t": start + i * 3600, "o": 100 + i, "h": 110 + i, "l": 90 + i,
                 "c": 105 + i, "v": 1.0, "sum": 10.0} for i in range(n)]

    def test_4h_aggregation(self):
        bars = synthesize(self._hourly(8), "1h", "4h")
        self.assertEqual(len(bars), 2)
        first = bars[0]
        self.assertEqual(first.o, 100)      # 第一根 open
        self.assertEqual(first.c, 108)      # 第四根 close
        self.assertEqual(first.h, max(110 + i for i in range(4)))
        self.assertEqual(first.l, min(90 + i for i in range(4)))
        self.assertEqual(first.v, 4.0)
        self.assertEqual(first.src, "synthetic")
        self.assertEqual(first.t % 14400, 0)

    def test_incomplete_bucket_dropped(self):
        bars = synthesize(self._hourly(6), "1h", "4h")  # 6 根 → 1 完整 + 半桶
        self.assertEqual(len(bars), 1)

    def test_non_multiple_rejected(self):
        with self.assertRaises(ValueError):
            synthesize(self._hourly(4), "4h", "6h")


class TestToBars(unittest.TestCase):
    def test_drops_unclosed_tail(self):
        now = 1_800_000_000
        rows = [{"t": now - 3600, "o": 1, "h": 2, "l": 0.5, "c": 1.5},
                {"t": now - 1800, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]  # 1h bar 未收线
        bars = to_bars(rows, "1h", now=now)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].src, "backfill")

    def test_sorted_output(self):
        rows = [{"t": 3, "o": 1, "h": 1, "l": 1, "c": 1},
                {"t": 1, "o": 1, "h": 1, "l": 1, "c": 1}]
        bars = to_bars(rows, "1d", drop_unclosed_tail=False)
        self.assertEqual([b.t for b in bars], [1, 3])

    def test_covered_span(self):
        rows = [{"t": 0}, {"t": 9 * DAY}]
        self.assertEqual(covered_span(rows, "1d")["n"], 2)
        self.assertAlmostEqual(covered_span(rows, "1d")["days"], 10.0)


if __name__ == "__main__":
    unittest.main()
