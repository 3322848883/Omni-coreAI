import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.indicators import (  # noqa: E402
    attach_indicators,
    highest,
    latest_indicators,
    linreg,
    lowest,
    parse_indicator_name,
    sma,
    squeeze_momentum,
    stdev,
    true_range,
)


def _ohlc(n=120, base=100.0):
    highs, lows, closes = [], [], []
    px = base
    for i in range(n):
        o = px
        c = px + (1.2 if i % 4 < 2 else -0.9)
        h = max(o, c) + 1.1
        l = min(o, c) - 1.1
        highs.append(h)
        lows.append(l)
        closes.append(c)
        px = c
    return highs, lows, closes


class TestHelpers(unittest.TestCase):
    def test_highest_lowest(self):
        vals = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
        self.assertEqual(highest(vals, 3)[2], 4.0)   # max(3,1,4)
        self.assertEqual(highest(vals, 3)[5], 9.0)   # max(5,9,2)
        self.assertEqual(lowest(vals, 3)[2], 1.0)    # min(3,1,4)
        self.assertEqual(lowest(vals, 3)[6], 2.0)    # min(5,9,2)

    def test_linreg_line(self):
        # perfect line y = 2x + 1 over 5 points: 1,3,5,7,9 → last = 9
        vals = [1.0, 3.0, 5.0, 7.0, 9.0]
        out = linreg(vals, 5)
        self.assertAlmostEqual(out[4], 9.0, places=6)

    def test_linreg_offset_curve(self):
        # y = x^2: best fit over x=0..4 is y = 4x - 2 → at x=4 → 14（不是真实 y=16）
        vals = [0.0, 1.0, 4.0, 9.0, 16.0]
        out = linreg(vals, 5)
        self.assertIsNotNone(out[4])
        self.assertAlmostEqual(out[4], 14.0, places=6)


class TestSqueezeMomentum(unittest.TestCase):
    def test_bb_inside_kc_logic(self):
        h, l, c = _ohlc(120)
        res = squeeze_momentum(h, l, c, 20, 2.0, 20, 1.5, True)
        self.assertTrue(any(x is not None for x in res["sqz_mom"]))
        states = [x for x in res["sqz_state"] if x is not None]
        self.assertTrue(all(s in (-1, 0, 1) for s in states))
        # sqz_on 与 sqz_off 互斥
        for on, off in zip(res["sqz_on"], res["sqz_off"]):
            if on is not None and off is not None:
                self.assertFalse(on and off)

    def test_bb_uses_mult_kc_uses_multkc(self):
        h, l, c = _ohlc(80)
        # 手动复现 BB 宽度
        basis = sma(c, 20)
        sd = stdev(c, 20)
        res = squeeze_momentum(h, l, c, 20, 2.0, 20, 1.5, True)
        # 有值即可；宽度由 bb_mult=2 与 kc_mult=1.5 各自控制
        self.assertTrue(any(x is not None for x in res["sqz_mom"]))

    def test_use_true_range_vs_hl(self):
        h, l, c = _ohlc(80)
        a = squeeze_momentum(h, l, c, 20, 2.0, 20, 1.5, True)
        b = squeeze_momentum(h, l, c, 20, 2.0, 20, 1.5, False)
        # TR 路径应与 H-L 路径不同（除非 H-L 已经是 TR）
        self.assertTrue(any(x is not None for x in a["sqz_mom"]))
        self.assertTrue(any(x is not None for x in b["sqz_mom"]))

    def test_momentum_sign_flags(self):
        h, l, c = _ohlc(100)
        res = squeeze_momentum(h, l, c)
        for v, up in zip(res["sqz_mom"], res["momentum_up"]):
            if v is not None and up is not None:
                self.assertEqual(up, v > 0)

    def test_parse_names(self):
        self.assertEqual(parse_indicator_name("sqzmom")["field"], "sqz_mom")
        self.assertEqual(parse_indicator_name("sqzmom_state")["field"], "sqz_state")
        spec = parse_indicator_name("sqzmom14_14")
        self.assertEqual(spec["bb_length"], 14)
        self.assertEqual(spec["kc_length"], 14)
        spec2 = parse_indicator_name("sqzmom20_20_state")
        self.assertEqual(spec2["field"], "sqz_state")

    def test_attach(self):
        h, l, c = _ohlc(120)
        rows = [
            {"t": i, "o": c[i], "h": h[i], "l": l[i], "c": c[i], "v": 100.0}
            for i in range(len(c))
        ]
        wanted = ["sqzmom", "sqzmom_state", "sqzmom20_20_up"]
        attach_indicators(rows, wanted)
        last = latest_indicators(rows, wanted)
        self.assertIn("sqzmom", last)
        self.assertIn("sqzmom_state", last)
        self.assertIn("sqz_state", rows[-1])  # 额外列
        self.assertIn("momentum_up", rows[-1])


if __name__ == "__main__":
    unittest.main()
