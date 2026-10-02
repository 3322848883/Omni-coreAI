import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.indicators import (  # noqa: E402
    attach_indicators,
    latest_indicators,
    macd,
    parse_indicator_name,
    rsi,
    rsi_bollinger,
    rsi_smoothed,
    sma,
)


class TestPineRSI(unittest.TestCase):
    def test_rsi_formula_matches_pine(self):
        # hand fixture: 6 closes → change bars 1..5; period=2
        closes = [10.0, 11.0, 10.5, 11.5, 11.0, 12.0]
        out = rsi(closes, 2)
        # gains/losses from change:
        # i=1: +1.0 / 0 | i=2: 0 / 0.5 | i=3: +1.0 / 0 | i=4: 0 / 0.5 | i=5: +1.0 / 0
        # RMA(len=2) of gains: seed idx1 = mean(1,0)=0.5 → maps to close idx2
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        # out[2] = rsi with up=0.5, down=mean(0,0.5)=0.25 → 100-100/(1+0.5/0.25)=66.66..
        self.assertAlmostEqual(out[2], 100.0 - 100.0 / (1.0 + 0.5 / 0.25), places=6)

    def test_rsi_all_up_is_100(self):
        closes = [float(i) for i in range(1, 20)]  # always rising
        out = rsi(closes, 14)
        self.assertEqual(out[-1], 100.0)

    def test_rsi_all_down_is_0(self):
        closes = [float(30 - i) for i in range(20)]
        out = rsi(closes, 14)
        self.assertEqual(out[-1], 0.0)

    def test_rsi_smooth_parse_and_values(self):
        closes = [100.0 + (i % 5) - 2 for i in range(60)]
        spec = parse_indicator_name("rsi14_ema")
        self.assertEqual(spec["kind"], "rsi_smooth")
        self.assertEqual(spec["ma_type"], "ema")
        raw = rsi(closes, 14)
        sm = rsi_smoothed(closes, 14, "ema", 14)
        self.assertTrue(any(x is not None for x in sm))
        # smoothed ≠ raw (different series)
        self.assertNotEqual(raw[-1], sm[-1])

    def test_rsi_bb(self):
        closes = [100.0 + ((i * 7) % 11) - 5 for i in range(80)]
        res = rsi_bollinger(closes, 14, 14, 2.0)
        self.assertTrue(any(x is not None for x in res["middle"]))
        up = res["upper"]
        lo = res["lower"]
        for u, m, l in zip(up, res["middle"], lo):
            if u is not None and m is not None and l is not None:
                self.assertGreaterEqual(u, m)
                self.assertLessEqual(l, m)


class TestPineMACD(unittest.TestCase):
    def test_macd_ema_default(self):
        closes = [100.0 + i * 0.5 + (i % 3) for i in range(60)]
        res = macd(closes, 12, 26, 9)
        self.assertTrue(any(x is not None for x in res["dif"]))
        self.assertTrue(any(x is not None for x in res["dea"]))
        # hist = dif - dea
        for d, s, h in zip(res["dif"], res["dea"], res["hist"]):
            if d is not None and s is not None:
                self.assertAlmostEqual(h, d - s)

    def test_macd_sma_option(self):
        closes = [100.0 + i * 0.5 + (i % 3) for i in range(60)]
        ema_res = macd(closes, 12, 26, 9, osc_type="ema", sig_type="ema")
        sma_res = macd(closes, 12, 26, 9, osc_type="sma", sig_type="sma")
        # SMA oscillator should differ from EMA oscillator
        self.assertNotEqual(ema_res["dif"][-1], sma_res["dif"][-1])

    def test_macd_parse_sma_suffix(self):
        spec = parse_indicator_name("macd12_26_9_sma")
        self.assertEqual(spec["osc_type"], "sma")
        self.assertEqual(spec["sig_type"], "sma")
        spec2 = parse_indicator_name("macd")
        self.assertEqual(spec2["osc_type"], "ema")
        spec3 = parse_indicator_name("macd_sma")
        self.assertEqual(spec3["osc_type"], "sma")


class TestAttachRSIMACD(unittest.TestCase):
    def test_attach_rsi_variants(self):
        rows = [
            {"t": i, "o": 100.0, "h": 101.0, "l": 99.0,
             "c": 100.0 + ((i * 3) % 7) - 3, "v": 50.0}
            for i in range(80)
        ]
        wanted = ["rsi14", "rsi14_sma", "rsi14_ema", "rsi14_bb", "macd", "macd_sma", "macd_hist"]
        attach_indicators(rows, wanted)
        last = latest_indicators(rows, wanted)
        for k in wanted:
            self.assertIn(k, last, k)
        self.assertTrue(0 <= last["rsi14"] <= 100)
        self.assertIn("rsi14_bb_upper", rows[-1])
        self.assertNotEqual(last["macd"], last["macd_sma"])  # EMA vs SMA differ


if __name__ == "__main__":
    unittest.main()
