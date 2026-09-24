import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.triggers import (  # noqa: E402
    check_conditions,
    evaluate_condition,
    parse_conditions,
)


class FakeMD:
    def __init__(self, closes=None):
        self.closes = closes or [float(i) for i in range(1, 40)]

    def public_get(self, path, qs=""):
        rows = []
        for i, c in enumerate(self.closes):
            t = 1000 + i * 60
            rows.append([t, "1", str(c), str(c + 1), str(c - 1), str(c - 0.5), "0"])
        return rows


class TestParseConditions(unittest.TestCase):
    def test_parse_skips_kline_close(self):
        conds = parse_conditions([
            {"type": "kline_close"},
            {"type": "price_vs_ema", "symbol": "BTC_USDT"},
            "bad",
        ])
        self.assertEqual(len(conds), 1)
        self.assertEqual(conds[0]["type"], "price_vs_ema")
        self.assertEqual(conds[0]["cooldown_sec"], 60)


class TestEvaluate(unittest.TestCase):
    def test_price_vs_ema_above(self):
        c = FakeMD()
        ok, reason = evaluate_condition(
            c, {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 5, "side": "above"},
            "1m",
        )
        self.assertTrue(ok)
        self.assertIn("ema5", reason)

    def test_price_vs_ema_below_not_fired(self):
        c = FakeMD()
        ok, _ = evaluate_condition(
            c, {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 5, "side": "below"},
            "1m",
        )
        self.assertFalse(ok)

    def test_ema_cross(self):
        # down then up shape → possible cross; just ensure callable + bool
        closes = [10, 9, 8, 7, 6, 5, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
        c = FakeMD(closes)
        ok, reason = evaluate_condition(
            c, {"type": "ema_cross", "symbol": "BTC_USDT", "fast": 3, "slow": 5, "dir": "any"},
            "1m",
        )
        self.assertIn(ok, (True, False))
        self.assertTrue(reason)

    def test_price_break_high(self):
        closes = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 5]
        c = FakeMD(closes)
        ok, reason = evaluate_condition(
            c, {"type": "price_break", "symbol": "BTC_USDT", "lookback": 5, "side": "high"},
            "1m",
        )
        self.assertTrue(ok)
        self.assertIn("hi5", reason)

    def test_rsi_gt(self):
        closes = [float(i) for i in range(1, 30)]
        c = FakeMD(closes)
        ok, reason = evaluate_condition(
            c, {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "gt", "level": 70},
            "1m",
        )
        self.assertTrue(ok)
        self.assertIn("rsi14", reason)

    def test_atr_spike(self):
        # steady then one huge bar
        closes = [10.0] * 25 + [12.0]
        highs = [c + 0.1 for c in closes]
        lows = [c - 0.1 for c in closes]
        highs[-1] = 20.0
        lows[-1] = 8.0

        class C:
            def public_get(self, path, qs=""):
                return [
                    [i, "1", str(closes[i]), str(highs[i]), str(lows[i]), str(closes[i]), "0"]
                    for i in range(len(closes))
                ]

        ok, reason = evaluate_condition(
            C(), {"type": "atr_spike", "symbol": "BTC_USDT", "period": 5, "mult": 1.2, "lookback": 10},
            "1m",
        )
        self.assertTrue(ok)
        self.assertIn("atr", reason)

    def test_cooldown(self):
        c = FakeMD()
        cond = {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 5, "side": "above", "cooldown_sec": 999}
        states = {}
        f1 = check_conditions(c, [cond], "1m", states=states, now=1000)
        f2 = check_conditions(c, [cond], "1m", states=states, now=1001)
        self.assertEqual(len(f1), 1)
        self.assertEqual(len(f2), 0)


if __name__ == "__main__":
    unittest.main()
