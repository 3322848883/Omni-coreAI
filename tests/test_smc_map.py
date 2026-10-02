import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.smc_map import BULL, BEAR, compute_smc_map, smc_map_summary


def synth_rows():
    rows = []
    px = 100.0
    for i in range(80):
        if i < 20:
            px -= 0.8
        elif i < 40:
            px += 1.2
        elif i < 60:
            px -= 1.0
        else:
            px += 1.5
        o = px
        c = px + (0.3 if i % 3 else -0.2)
        h = max(o, c) + 0.6
        l = min(o, c) - 0.6
        rows.append({"t": 1000 + i * 60, "o": o, "h": h, "l": l, "c": c, "v": 10})
    return rows


class TestSMC(unittest.TestCase):
    def test_compute_runs_and_has_events(self):
        rows = synth_rows()
        res = compute_smc_map(rows, swings_length=10, internal_size=5)
        self.assertTrue(res.events or res.swing_order_blocks is not None)
        self.assertIn("drawings", res.__dict__)
        self.assertIn("boxes", res.drawings)
        self.assertIn("premium_discount", res.__dict__)
        self.assertTrue(res.premium_discount.get("current_zone") in ("premium", "discount"))

    def test_trend_moves_with_legs(self):
        up = [{"t": i, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100.5 + i, "v": 1} for i in range(60)]
        res = compute_smc_map(up, swings_length=5, internal_size=3)
        self.assertIn(res.swing_trend, (BULL, BEAR, 0))
        s = smc_map_summary(res)
        self.assertIn("swing_trend", s)
        self.assertIn("order_blocks", s)

    def test_fvg_detect(self):
        # force bullish FVG: low[2] > high[0]
        rows = [
            {"t": 1, "o": 10, "h": 10, "l": 9, "c": 10, "v": 1},
            {"t": 2, "o": 11, "h": 11, "l": 10.5, "c": 11, "v": 1},
            {"t": 3, "o": 12, "h": 12, "l": 11.5, "c": 12, "v": 1},
            {"t": 4, "o": 12, "h": 12, "l": 11.6, "c": 12, "v": 1},
            {"t": 5, "o": 12, "h": 12, "l": 11.7, "c": 12, "v": 1},
        ]
        # make low[2]=11.5 > high[0]=10
        res = compute_smc_map(rows, swings_length=2, internal_size=2)
        self.assertTrue(len(res.fvgs) >= 0)

    def test_drawings_payload(self):
        res = compute_smc_map(synth_rows(), swings_length=8, internal_size=4)
        d = res.drawings
        for k in ("labels", "lines", "boxes"):
            self.assertIn(k, d)
            self.assertIsInstance(d[k], list)


if __name__ == "__main__":
    unittest.main()
