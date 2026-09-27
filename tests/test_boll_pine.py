import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.indicators import (  # noqa: E402
    attach_indicators,
    boll,
    latest_indicators,
    parse_indicator_name,
    sma,
    stdev,
)


class TestPineBollinger(unittest.TestCase):
    def test_basis_sma_matches_pine_formula(self):
        closes = [10.0, 11.0, 12.0, 11.5, 12.5, 13.0, 12.0, 13.5, 14.0, 13.0,
                  12.5, 13.5, 14.5, 15.0, 14.0, 15.5, 16.0, 15.0, 16.5, 17.0]
        period, mult = 5, 2.0
        res = boll(closes, period, mult, ma_type="sma")
        basis = sma(closes, period)
        sd = stdev(closes, period)
        for i in range(period - 1, len(closes)):
            self.assertAlmostEqual(res["middle"][i], basis[i])
            self.assertAlmostEqual(res["upper"][i], basis[i] + mult * sd[i])
            self.assertAlmostEqual(res["lower"][i], basis[i] - mult * sd[i])

    def test_stdev_is_population(self):
        # values 1..5: mean=3, pop var = (4+1+0+1+4)/5 = 2 → sd = sqrt(2)
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        out = stdev(vals, 5)
        self.assertAlmostEqual(out[4], 2.0 ** 0.5, places=9)

    def test_basis_ma_type_changes_bands(self):
        closes = [100.0 + ((i * 7) % 13) - 6 + i * 0.3 for i in range(60)]
        sma_r = boll(closes, 20, 2.0, ma_type="sma")
        ema_r = boll(closes, 20, 2.0, ma_type="ema")
        rma_r = boll(closes, 20, 2.0, ma_type="rma")
        self.assertNotEqual(sma_r["middle"][-1], ema_r["middle"][-1])
        self.assertNotEqual(sma_r["middle"][-1], rma_r["middle"][-1])
        # upper-lower 宽度由 stdev 决定，不同 basis 下宽度应相同（同 k、同 src）
        for a, b in zip(sma_r["upper"], ema_r["upper"]):
            if a is not None and b is not None and ema_r["middle"] is not None:
                pass
        s_width = sma_r["upper"][-1] - sma_r["lower"][-1]
        e_width = ema_r["upper"][-1] - ema_r["lower"][-1]
        self.assertAlmostEqual(s_width, e_width, places=6)

    def test_parse_boll_variants(self):
        cases = {
            "boll20": ("sma", 20, 2.0),
            "boll20_2": ("sma", 20, 2.0),
            "boll20_ema": ("ema", 20, 2.0),
            "boll20_rma": ("rma", 20, 2.0),
            "boll20_smma": ("rma", 20, 2.0),
            "boll20_wma": ("wma", 20, 2.0),
            "boll20_vwma": ("vwma", 20, 2.0),
            "boll20_ema_2.5": ("ema", 20, 2.5),
            "boll_upper": ("sma", 20, 2.0),
        }
        for name, (mat, per, k) in cases.items():
            spec = parse_indicator_name(name)
            self.assertEqual(spec["kind"], "boll", name)
            self.assertEqual(spec["ma_type"], mat, name)
            self.assertEqual(spec["period"], per, name)
            self.assertAlmostEqual(spec["k"], k, name)

    def test_attach_boll_variants_no_clobber(self):
        rows = [
            {"t": i, "o": 100.0, "h": 101.0, "l": 99.0,
             "c": 100.0 + ((i * 5) % 11) - 5 + i * 0.2, "v": 20.0}
            for i in range(80)
        ]
        wanted = ["boll20", "boll20_ema", "boll_upper", "boll20_ema_upper"]
        attach_indicators(rows, wanted)
        last = latest_indicators(rows, wanted)
        # boll20 是 SMA 基线；boll20_ema 是 EMA 基线 — 不能被覆盖成同一个
        self.assertNotEqual(last["boll20"], last["boll20_ema"])
        self.assertIn("boll_upper", last)
        self.assertIn("boll20_ema_upper", last)


if __name__ == "__main__":
    unittest.main()
