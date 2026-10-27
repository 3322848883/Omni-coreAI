# -*- coding: utf-8 -*-
"""指标配置层（pa-data-source/indicator_config.py）：列名规范 / 预热 / 指纹。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))

from indicator_config import (  # noqa: E402
    DEFAULT_INDICATORS,
    indicator_columns,
    parse_indicators,
    spec_hash,
    warmup_bars,
)


class TestColumns(unittest.TestCase):
    def test_default_specs_give_11_columns(self):
        cols = indicator_columns(parse_indicators(None))
        self.assertEqual(cols, [
            "ema20", "ema50", "ema200",
            "atr14", "rsi14",
            "macd", "macd_dea", "macd_hist",
            "boll20", "boll20_upper", "boll20_lower",
        ])

    def test_custom_macd_params_get_suffix(self):
        specs = parse_indicators([{"macd": [5, 35, 5]}])
        self.assertEqual(specs[0].columns,
                         ("macd_5_35_5", "macd_5_35_5_dea", "macd_5_35_5_hist"))

    def test_custom_boll_k_uses_p_form(self):
        specs = parse_indicators([{"boll": [20, 2.5]}])
        self.assertEqual(specs[0].columns,
                         ("boll20_2p5", "boll20_2p5_upper", "boll20_2p5_lower"))

    def test_only_configured_indicators_produce_columns(self):
        cols = indicator_columns(parse_indicators([{"ema": [200]}]))
        self.assertEqual(cols, ["ema200"])


class TestValidation(unittest.TestCase):
    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            parse_indicators([{"supertrend": [10, 3]}])

    def test_period_too_small_raises(self):
        with self.assertRaises(ValueError):
            parse_indicators([{"ema": [1]}])

    def test_bad_macd_params_raise(self):
        with self.assertRaises(ValueError):
            parse_indicators([{"macd": [12, 26]}])

    def test_duplicate_deduped(self):
        specs = parse_indicators([{"ema": [20]}, {"ema": [20]}, {"atr": [14]}])
        self.assertEqual(len(specs), 2)

    def test_string_form_rejected_without_params(self):
        with self.assertRaises(ValueError):
            parse_indicators(["ema"])

    def test_none_yields_defaults(self):
        self.assertEqual(len(parse_indicators(None)), len(DEFAULT_INDICATORS))


class TestWarmup(unittest.TestCase):
    def test_long_ema_needs_5x(self):
        self.assertGreaterEqual(warmup_bars(parse_indicators([{"ema": [200]}])), 1000)

    def test_short_period_values(self):
        # 与读取端 indicator_warmup_bars 的口径一致：短周期 = period + 5
        self.assertEqual(warmup_bars(parse_indicators([{"atr": [14]}])), 19)
        self.assertEqual(warmup_bars(parse_indicators([{"rsi": [14]}])), 19)
        self.assertEqual(warmup_bars(parse_indicators([{"ema": [20]}])), 25)
        self.assertEqual(warmup_bars(parse_indicators([{"boll": [20, 2]}])), 25)

    def test_macd_covers_slow_plus_signal(self):
        self.assertGreaterEqual(warmup_bars(parse_indicators([{"macd": [12, 26, 9]}])), 26 + 9 + 5)


class TestHash(unittest.TestCase):
    def test_hash_is_order_insensitive(self):
        a = parse_indicators([{"ema": [20, 50]}, {"atr": [14]}])
        b = parse_indicators([{"atr": [14]}, {"ema": [50, 20]}])
        self.assertEqual(spec_hash(a), spec_hash(b))

    def test_hash_changes_with_params(self):
        a = parse_indicators([{"ema": [20]}])
        b = parse_indicators([{"ema": [21]}])
        self.assertNotEqual(spec_hash(a), spec_hash(b))


if __name__ == "__main__":
    unittest.main()
