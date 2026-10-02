import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.indicators import (  # noqa: E402
    IndicatorNameError,
    adx,
    attach_indicators,
    atr,
    cci,
    latest_indicators,
    mfi,
    obv,
    parse_indicator_name,
    stochastic,
    supertrend,
    true_range,
    vwap,
    williams_r,
)


def _ohlc(n=60, base=100.0):
    highs, lows, closes, vols = [], [], [], []
    px = base
    for i in range(n):
        o = px
        c = px + (1.0 if i % 3 else -0.8)
        h = max(o, c) + 0.9
        l = min(o, c) - 0.9
        highs.append(h)
        lows.append(l)
        closes.append(c)
        vols.append(100.0 + (i % 7) * 10)
        px = c
    return highs, lows, closes, vols


class TestPineATR(unittest.TestCase):
    def test_true_range_includes_bar0(self):
        tr = true_range([10.0, 12.0], [8.0, 9.0], [9.0, 11.0])
        self.assertEqual(tr[0], 2.0)  # Pine ta.tr(true) bar0 = h-l
        self.assertEqual(tr[1], 3.0)

    def test_atr_default_rma(self):
        highs, lows, closes, _ = _ohlc(30)
        out = atr(highs, lows, closes, 14)
        self.assertIsNone(out[12])
        self.assertIsNotNone(out[13])
        self.assertGreater(out[13], 0)

    def test_atr_smoothing_name_parse(self):
        self.assertEqual(parse_indicator_name("atr14")["smoothing"], "rma")
        self.assertEqual(parse_indicator_name("atr14_ema")["smoothing"], "ema")
        self.assertEqual(parse_indicator_name("atr20_sma")["smoothing"], "sma")
        self.assertEqual(parse_indicator_name("atr10_wma")["smoothing"], "wma")


class TestNewIndicators(unittest.TestCase):
    def test_parse_names(self):
        for name in ("stoch14", "stoch_k14", "stoch_d14", "cci20", "wr14",
                     "mfi14", "adx14", "vwap", "vwap20", "obv",
                     "supertrend", "supertrend10", "supertrend10_2"):
            spec = parse_indicator_name(name)
            self.assertIn("kind", spec, name)

    def test_stochastic_range(self):
        h, l, c, _ = _ohlc(40)
        res = stochastic(h, l, c, k_period=14, k_smooth=3, d_period=3)
        vals = [x for x in res["k"] if x is not None]
        self.assertTrue(vals)
        self.assertTrue(all(0 <= v <= 100 for v in vals))
        self.assertTrue(any(x is not None for x in res["d"]))

    def test_cci_sign(self):
        h, l, c, _ = _ohlc(40)
        out = cci(h, l, c, 20)
        self.assertTrue(any(x is not None for x in out))

    def test_williams_r_range(self):
        h, l, c, _ = _ohlc(40)
        out = williams_r(h, l, c, 14)
        vals = [x for x in out if x is not None]
        self.assertTrue(vals)
        self.assertTrue(all(-100 <= v <= 0 for v in vals))

    def test_mfi_range(self):
        h, l, c, v = _ohlc(40)
        out = mfi(h, l, c, v, 14)
        vals = [x for x in out if x is not None]
        self.assertTrue(vals)
        self.assertTrue(all(0 <= x <= 100 for x in vals))

    def test_adx_output(self):
        h, l, c, _ = _ohlc(60)
        res = adx(h, l, c, 14)
        self.assertTrue(any(x is not None for x in res["adx"]))
        self.assertTrue(any(x is not None for x in res["plus_di"]))

    def test_vwap_cumulative_and_rolling(self):
        h, l, c, v = _ohlc(30)
        cum = vwap(h, l, c, v, 0)
        self.assertIsNotNone(cum[-1])
        roll = vwap(h, l, c, v, 10)
        self.assertTrue(any(x is not None for x in roll))

    def test_obv_sign_changes(self):
        c = [10.0, 11.0, 10.5, 10.5, 12.0]
        v = [10.0, 20.0, 30.0, 40.0, 50.0]
        out = obv(c, v)
        self.assertEqual(out[0], 0.0)
        self.assertEqual(out[1], 20.0)   # up: +20
        self.assertEqual(out[2], -10.0)  # down: -30 → 20-30
        self.assertEqual(out[3], -10.0)  # unchanged
        self.assertEqual(out[4], 40.0)   # up: +50 → -10+50

    def test_supertrend_direction(self):
        h, l, c, _ = _ohlc(80)
        res = supertrend(h, l, c, 10, 3.0)
        self.assertTrue(any(x is not None for x in res["supertrend"]))
        dirs = [x for x in res["direction"] if x is not None]
        self.assertTrue(all(d in (1, -1) for d in dirs))

    def test_attach_and_latest(self):
        h, l, c, v = _ohlc(60)
        rows = [
            {"t": i, "o": c[i], "h": h[i], "l": l[i], "c": c[i], "v": v[i]}
            for i in range(len(c))
        ]
        wanted = ["atr14", "atr14_ema", "stoch14", "cci20", "wr14",
                  "mfi14", "adx14", "vwap", "obv", "supertrend10"]
        attach_indicators(rows, wanted)
        last = latest_indicators(rows, wanted)
        for k in wanted:
            self.assertIn(k, last, k)
        # ATR rma vs ema 平滑不同
        self.assertNotEqual(last["atr14"], last["atr14_ema"])

    def test_unknown_still_raises(self):
        with self.assertRaises(IndicatorNameError):
            parse_indicator_name("foo_bar_99")


if __name__ == "__main__":
    unittest.main()
