import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.smc_map import compute_smc_map, smc_map_summary  # noqa: E402
from omnialpha.strategist.smc_events import (  # noqa: E402
    compute_smc_events,
    pivothigh,
    pivotlow,
    smc_events_summary,
)


def synth_rows(n=120, base=100.0):
    rows = []
    px = base
    for i in range(n):
        if i < 30:
            px -= 0.6
        elif i < 60:
            px += 1.0
        elif i < 90:
            px -= 0.8
        else:
            px += 1.2
        o = px
        c = px + (0.4 if i % 3 else -0.3)
        h = max(o, c) + 0.8
        l = min(o, c) - 0.8
        rows.append({"t": 1_700_000_000 + i * 900, "o": o, "h": h, "l": l, "c": c, "v": 10.0})
    return rows


class TestTwoSetsCoexist(unittest.TestCase):
    """smc_map 与 smc_events 并存，互不替换。"""

    def test_both_importable(self):
        rows = synth_rows()
        a = compute_smc_map(rows, swings_length=10, internal_size=5)
        b = compute_smc_events(rows)
        self.assertTrue(smc_map_summary(a))
        self.assertTrue(smc_events_summary(b))
        self.assertTrue(hasattr(a, "swing_trend"))
        self.assertTrue(hasattr(b, "trend"))

    def test_smc_map_still_works(self):
        rows = synth_rows(80)
        r = compute_smc_map(rows, swings_length=10, internal_size=5)
        self.assertIn("premium_discount", r.__dict__)
        s = smc_map_summary(r)
        self.assertIn("swing_trend", s)
        self.assertIn("order_blocks", s)


class TestPivots(unittest.TestCase):
    def test_pivothigh_pivotlow(self):
        highs = [1, 2, 5, 2, 1, 1, 3, 2]
        lows = [0, 1, 4, 1, 0, 1, 2, 1]
        ph = pivothigh(highs, 2, 2)
        pl = pivotlow(lows, 2, 2)
        self.assertEqual(ph[4], 5)
        self.assertEqual(pl[6], 0)


class TestEventsAlgo(unittest.TestCase):
    def test_computes_and_has_events(self):
        rows = synth_rows(150)
        res = compute_smc_events(rows)
        self.assertTrue(res.events or res.trend in (-1, 0, 1))
        self.assertIn("boxes", res.drawings)
        self.assertIn("lines", res.drawings)
        self.assertTrue(res.premium_discount.get("current_zone") in ("premium", "discount"))

    def test_ob_breaker_flags(self):
        rows = synth_rows(150)
        res = compute_smc_events(rows)
        for ob in res.bull_obs + res.bear_obs:
            self.assertTrue(ob.bull in (True, False))
            self.assertGreaterEqual(ob.top, ob.btm)
            self.assertIn(ob.is_breaker, (True, False))

    def test_fvg_zones_valid(self):
        rows = synth_rows(150)
        res = compute_smc_events(rows)
        for f in res.bull_fvgs + res.bear_fvgs:
            self.assertGreater(f.top, f.btm)

    def test_summary_shape(self):
        rows = synth_rows(120)
        res = compute_smc_events(rows)
        s = smc_events_summary(res)
        for k in ("trend", "events", "order_blocks", "fvgs", "premium_discount", "sweeps"):
            self.assertIn(k, s)

    def test_mitigate_methods(self):
        rows = synth_rows(120)
        for m in ("close", "wick", "avg"):
            res = compute_smc_events(rows, ob_mitigate=m, fvg_mitigate=m)
            self.assertIsInstance(res.bull_obs, list)

    def test_ob_modes(self):
        rows = synth_rows(120)
        for mode in ("length", "full"):
            res = compute_smc_events(rows, ob_mode=mode)
            self.assertIsInstance(res.bull_obs, list)


class TestAllFeaturesOn(unittest.TestCase):
    """默认功能全部打开（含 Pine 默认关的项）。"""

    def test_defaults_enable_all(self):
        rows = synth_rows(150)
        res = compute_smc_events(rows)
        d = res.drawings
        self.assertIn("trend_color", d)
        self.assertIn("last_event", d)
        tags = {ln.get("tag") for ln in d.get("lines", [])}
        self.assertIn("ob_mid", tags)
        self.assertIn("fvg_mid", tags)
        types = {b.get("type") for b in d.get("boxes", [])}
        self.assertIn("ob_activity", types)
        for ob in res.bull_obs + res.bear_obs:
            self.assertIn(ob.is_breaker, (True, False))
        for f in res.bull_fvgs + res.bear_fvgs:
            self.assertIn(f.is_breaker, (True, False))
            self.assertIn(f.is_raid, (True, False))

    def test_smc_map_show_all(self):
        from omnialpha.strategist.smc_map import SHOW_ALL
        self.assertTrue(all(SHOW_ALL.values()))


if __name__ == "__main__":
    unittest.main()
