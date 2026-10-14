# -*- coding: utf-8 -*-
"""守护扫描的币数上限可配 + 超限可见（N4）。

`_SCAN_SYMBOL_CAP = 12` 曾是模块常量：bot 加币超过上限只会被**静默少扫**，
运维无从调、也无从知道「配了却没被守护」。现在上限是 bot 级配置
`max_scan_symbols`（默认 12 = 存量行为），超限同时落 `skipped.over_cap`
与 observability 计数。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha import observability  # noqa: E402
from omnialpha.config import BotConfig, load_bot_config  # noqa: E402
from omnialpha.config_keys import SCHEMA  # noqa: E402
from omnialpha.watcher import _SCAN_SYMBOL_CAP, guard_scan_set  # noqa: E402


def _bot(n_symbols: int, cap: int | None = None) -> BotConfig:
    syms = [f"S{i}_USDT" for i in range(n_symbols)]
    b = BotConfig(bot_id="cap-bot", symbols=syms)
    if cap is not None:
        b.max_scan_symbols = cap
    return b


class TestScanCap(unittest.TestCase):
    def setUp(self):
        observability.reset_counts()

    def tearDown(self):
        observability.reset_counts()

    def test_under_cap_scans_everything(self):
        out = guard_scan_set(_bot(3), client=None)
        self.assertEqual(out["symbols"], ["S0_USDT", "S1_USDT", "S2_USDT"])
        self.assertEqual(out["skipped"], [])

    def test_default_cap_matches_legacy_constant(self):
        """未配置时行为与旧的模块常量逐字一致（存量不受影响）。"""
        out = guard_scan_set(_bot(_SCAN_SYMBOL_CAP + 3), client=None)
        self.assertEqual(len(out["symbols"]), _SCAN_SYMBOL_CAP)
        self.assertEqual([s["reason"] for s in out["skipped"]],
                         ["over_cap"] * 3)

    def test_configured_cap_is_honoured(self):
        out = guard_scan_set(_bot(10, cap=4), client=None)
        self.assertEqual(len(out["symbols"]), 4)
        self.assertEqual(len(out["skipped"]), 6)
        self.assertTrue(all(s["reason"] == "over_cap" for s in out["skipped"]))

    def test_larger_cap_allows_more(self):
        out = guard_scan_set(_bot(20, cap=20), client=None)
        self.assertEqual(len(out["symbols"]), 20)
        self.assertEqual(out["skipped"], [])

    def test_over_cap_is_counted(self):
        guard_scan_set(_bot(15, cap=10), client=None)
        self.assertEqual(observability.counts().get("watcher.guard_scan_set.over_cap"), 1)

    def test_under_cap_is_not_counted(self):
        guard_scan_set(_bot(3), client=None)
        self.assertNotIn("watcher.guard_scan_set.over_cap", observability.counts())


class TestScanCapConfig(unittest.TestCase):
    def test_default_is_twelve(self):
        self.assertEqual(BotConfig(bot_id="x").max_scan_symbols, 12)

    def test_yaml_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cap-bot.yaml"
            p.write_text("bot_id: cap-bot\nenv: paper\nsymbols: [BTC_USDT]\n"
                         "label_prefix: cb\nmax_scan_symbols: 25\n", encoding="utf-8")
            self.assertEqual(load_bot_config(p).max_scan_symbols, 25)

    def test_missing_key_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cap-bot.yaml"
            p.write_text("bot_id: cap-bot\nenv: paper\nsymbols: [BTC_USDT]\n"
                         "label_prefix: cb\n", encoding="utf-8")
            self.assertEqual(load_bot_config(p).max_scan_symbols, 12)

    def test_whitelisted(self):
        self.assertIn("max_scan_symbols", SCHEMA[""])


if __name__ == "__main__":
    unittest.main()
