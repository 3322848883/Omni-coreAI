import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.indicators import (
    ema,
    ema_boll,
    ema_smooth,
    rma,
    sma,
    stdev,
    vwma,
    wma,
    attach_indicators,
    parse_indicator_name,
    IndicatorNameError,
)


class TestNewMAs(unittest.TestCase):
    def test_rma_matches_wilder(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        out = rma(vals, 3)
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 2.0)  # seed mean
        self.assertAlmostEqual(out[3], (2.0 * 2 + 4.0) / 3)
        self.assertAlmostEqual(out[4], (out[3] * 2 + 5.0) / 3)

    def test_wma_weights(self):
        vals = [1.0, 2.0, 3.0]
        out = wma(vals, 3)
        self.assertAlmostEqual(out[2], (1 * 1 + 2 * 2 + 3 * 3) / 6)

    def test_vwma(self):
        p = [10.0, 20.0, 30.0]
        v = [1.0, 1.0, 2.0]
        out = vwma(p, v, 3)
        self.assertAlmostEqual(out[2], (10 + 20 + 60) / 4)

    def test_stdev(self):
        vals = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        out = stdev(vals, 8)
        self.assertIsNotNone(out[7])
        self.assertAlmostEqual(out[7], 2.0)

    def test_ema_smooth_ema(self):
        vals = list(range(1, 40))
        s = ema_smooth(vals, 20, 14, "ema")
        self.assertIsNone(s[18])
        self.assertIsNotNone(s[25])

    def test_ema_boll_upper_gt_middle(self):
        vals = list(range(1, 50))
        b = ema_boll(vals, 20, 14, 2.0, "ema")
        self.assertGreater(b["upper"][-1], b["middle"][-1])
        self.assertLess(b["lower"][-1], b["middle"][-1])

    def test_parse_names(self):
        self.assertEqual(parse_indicator_name("rma14")["kind"], "rma")
        self.assertEqual(parse_indicator_name("wma20")["kind"], "wma")
        self.assertEqual(parse_indicator_name("ema_smooth")["kind"], "ema_smooth")
        self.assertEqual(parse_indicator_name("ema_boll")["kind"], "ema_boll")
        with self.assertRaises(IndicatorNameError):
            parse_indicator_name("nope")

    def test_attach_indicators_rows(self):
        rows = [{"t": i, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.0 + i * 0.1,
                 "v": 10.0 + i} for i in range(40)]
        out = attach_indicators(rows, ["ema20", "rma14", "wma20", "vwma10", "ema_smooth", "ema_boll"])
        self.assertIsNotNone(out[-1].get("ema20"))
        self.assertIsNotNone(out[-1].get("rma14"))
        self.assertIsNotNone(out[-1].get("wma20"))
        self.assertIsNotNone(out[-1].get("vwma10"))
        self.assertIsNotNone(out[-1].get("ema_smooth"))
        self.assertIsNotNone(out[-1].get("ema_boll"))


if __name__ == "__main__":
    unittest.main()
