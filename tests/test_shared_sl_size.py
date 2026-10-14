# -*- coding: utf-8 -*-
"""共享 SL 的张数语义（F1）。

`Intent.sl_size_override` 此前是**写了没人读**的死字段：`schema.expand_signal`
在 `sl_scope: shared` 的末层写入 `total_size`，而 `executor._open` 只读
`tp_size_override`。于是「全梯共用一条 SL」只能靠 `_plan_shared_sl` 的同价归并
逐档累加**间接**达成。现在 `_open` 显式读取它，且仍受 `_sl_cover_cap` 钳制在
真实敞口内（不会挂出超配保护单）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import expand_signal, parse_signal  # noqa: E402


def _grid(sl_scope: str, *, sl=49000, sizes=(1, 2, 3)) -> list:
    sig = parse_signal({
        "action": "grid", "symbol": "BTC_USDT", "side": "long",
        "tp": 51000, "sl": sl,
        "tp_scope": "shared", "sl_scope": sl_scope,
        "levels": [{"price": 50000 - i * 100, "size": s} for i, s in enumerate(sizes)],
    })
    return expand_signal(sig)


class TestExpandSignalWritesOverride(unittest.TestCase):
    def test_shared_sl_only_on_last_level(self):
        out = _grid("shared")
        self.assertEqual([i.sl_size_override for i in out], [None, None, 6])

    def test_total_is_sum_of_levels(self):
        out = _grid("shared", sizes=(5, 5))
        self.assertEqual(out[-1].sl_size_override, 10)

    def test_per_level_has_no_override(self):
        out = _grid("per_level")
        self.assertEqual([i.sl_size_override for i in out], [None, None, None])

    def test_no_sl_means_no_override(self):
        sig = parse_signal({
            "action": "grid", "symbol": "BTC_USDT", "side": "long",
            "tp": 51000, "sl_scope": "shared",
            "levels": [{"price": 50000, "size": 1}],
        })
        self.assertIsNone(expand_signal(sig)[0].sl_size_override)

    def test_tp_override_still_works(self):
        """F1 只补 SL 一侧；TP 一侧本来就是通的（防回归）。"""
        out = _grid("per_level")
        self.assertEqual(out[-1].tp_size_override, 6)


class TestExecutorConsumesOverride(unittest.TestCase):
    """`_open` 必须真的读这个字段（否则又是一次「写了没人读」）。"""

    def test_open_reads_sl_size_override(self):
        src = (ROOT / "omnialpha" / "executor.py").read_text(encoding="utf-8")
        self.assertIn("intent.sl_size_override", src,
                      "_open 未读取 Intent.sl_size_override")

    def test_cover_cap_still_clamps(self):
        """override 再大也不会挂出超过真实敞口的保护单。"""
        from omnialpha.schema import parse_signal as _ps

        class _Client:
            def banner(self):
                return ""

            def get_positions(self):
                return [{"contract": "BTC_USDT", "mode": "dual",
                         "size": 2, "entry_price": 50000}]

            def get_contract(self, symbol):
                class _M:
                    quanto_multiplier = 0.0001
                    order_size_min = 1
                    price_tick = 0.1
                    order_price_round = 0.1
                    min_notional_usd = 1.0
                return _M()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            ex = Executor(_Client(), symbols_whitelist=["BTC_USDT"],
                          root=Path(td), bot_id="b1")
            cap = ex._sl_cover_cap("BTC_USDT", "long")
            self.assertEqual(cap, 2, "上限应等于真实敞口")
            meta = _Client().get_contract("BTC_USDT")
            it = parse_signal({
                "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
                "price": 50000, "sl": 49000, "tp": 51000,
            }).intents[0]
            it.sl_size_override = 999
            size, ids = ex._resolve_sl_size(it, "long", 1, meta)
            self.assertLessEqual(size, cap, "override 再大也不得超过敞口上限")
            self.assertEqual(ids, [])

    def test_per_level_path_unchanged(self):
        """非 override 路径逐字不变（第 ③ 步只在 override 时执行）。"""
        class _Client:
            def banner(self):
                return ""

            def get_positions(self):
                return [{"contract": "BTC_USDT", "mode": "dual",
                         "size": 2, "entry_price": 50000}]

            def get_contract(self, symbol):
                class _M:
                    quanto_multiplier = 0.0001
                    order_size_min = 1
                    price_tick = 0.1
                    order_price_round = 0.1
                    min_notional_usd = 1.0
                return _M()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            ex = Executor(_Client(), symbols_whitelist=["BTC_USDT"],
                          root=Path(td), bot_id="b1")
            it = parse_signal({
                "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
                "price": 50000, "sl": 49000, "tp": 51000,
            }).intents[0]
            self.assertIsNone(it.sl_size_override)
            size, ids = ex._resolve_sl_size(it, "long", 7, _Client().get_contract("BTC_USDT"))
            self.assertEqual(size, 7, "无 override → 本档张数原样")
            self.assertEqual(ids, [])


if __name__ == "__main__":
    unittest.main()
