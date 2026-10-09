"""symbol 身份解析（T4）：单一实现 + 紧凑写法。

为什么单独一个测试文件：`resolve_symbol` 的判据此前有**两份实现**
（`omnialpha/gate_client.py` 与 `pa-data-source/kline_watcher.py`），语义不同，
且都处理不了紧凑写法（`BTCUSDT` → 静默产出 `BTCUSDT_USDT`，合约不存在 → 查询空）。
这里既钉住归一规则，也钉住「采集侧与 bot 侧是**同一份**实现」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PA = ROOT / "pa-data-source"
if str(PA) not in sys.path:
    sys.path.insert(0, str(PA))

from omnialpha.gate_client import SYMBOL_MAP, GateApiError, resolve_symbol  # noqa: E402

# 现有写法（改前改后必须逐字相同）
LEGACY_CASES = [
    ("BTC", "BTC_USDT"),
    ("btc", "BTC_USDT"),
    ("BTC_USDT", "BTC_USDT"),
    ("btc_usdt", "BTC_USDT"),
    ("ETH", "ETH_USDT"),
    ("SOL", "SOL_USDT"),
    ("XAU", "XAU_USDT"),
    ("XAG", "XAG_USDT"),
    ("AU", "XAU_USDT"),
    ("AG", "XAG_USDT"),
    ("XAU_USDT", "XAU_USDT"),
    ("AU_USDT", "XAU_USDT"),
    ("DOGE", "DOGE_USDT"),
    ("PEPE_USDT", "PEPE_USDT"),
]

# 紧凑写法（本次新增）
COMPACT_CASES = [
    ("BTCUSDT", "BTC_USDT"),
    ("DOGEUSDT", "DOGE_USDT"),
    ("pepeusdt", "PEPE_USDT"),
    ("XAGUSDT", "XAG_USDT"),
    ("DOGEUSDC", "DOGE_USDC"),
]


class TestResolveSymbol(unittest.TestCase):
    def test_legacy_forms_unchanged(self):
        for raw, want in LEGACY_CASES:
            self.assertEqual(resolve_symbol(raw), want, raw)

    def test_compact_forms(self):
        for raw, want in COMPACT_CASES:
            self.assertEqual(resolve_symbol(raw), want, raw)

    def test_whitespace_tolerated(self):
        self.assertEqual(resolve_symbol("  btc  "), "BTC_USDT")

    def test_underscore_form_wins_over_compact_suffix(self):
        """`BTC_USDT` 不能被紧凑分支二次拼接成 `BTC__USDT`。"""
        self.assertEqual(resolve_symbol("BTC_USDT"), "BTC_USDT")
        self.assertEqual(resolve_symbol("PEPE_USDC"), "PEPE_USDC")

    def test_compact_and_underscore_agree(self):
        """同一标的两种写法必须落到同一个合约（否则两处判据又开始漂移）。"""
        for base in ("BTC", "ETH", "SOL", "XAU", "DOGE", "PEPE"):
            self.assertEqual(
                resolve_symbol(base + "USDT"), resolve_symbol(base + "_USDT"), base
            )

    def test_alias_base_keeps_legacy_quote_behavior(self):
        """别名表里的 base 一律落到它的 USDT 合约（`SOL_USDC` 的老行为），紧凑写法照抄。"""
        for raw in ("SOL_USDC", "SOLUSDC"):
            self.assertEqual(resolve_symbol(raw), "SOL_USDT", raw)

    def test_idempotent(self):
        for raw, _ in LEGACY_CASES + COMPACT_CASES:
            once = resolve_symbol(raw)
            self.assertEqual(resolve_symbol(once), once, raw)

    def test_empty_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(GateApiError):
                resolve_symbol(bad)

    def test_symbol_map_kept(self):
        """别名表仍在（`AU`/`AG` 这类老写法靠它）。"""
        self.assertEqual(SYMBOL_MAP["AU"], "XAU_USDT")


class TestSingleImplementation(unittest.TestCase):
    """采集侧必须**引用**同一份实现，而不是自己再写一遍。"""

    def test_kline_watcher_reuses_gate_client_impl(self):
        import kline_watcher as kw

        self.assertIs(
            kw.resolve_symbol, resolve_symbol,
            "pa-data-source/kline_watcher.py 必须 import 同一份 resolve_symbol（D2）",
        )

    def test_collector_side_same_results(self):
        import kline_watcher as kw

        for raw, want in LEGACY_CASES + COMPACT_CASES:
            self.assertEqual(kw.resolve_symbol(raw), want, raw)

    def test_no_second_copy_in_source(self):
        """源码级反断言：采集侧不得再出现第二份 `def resolve_symbol`。"""
        src = (PA / "kline_watcher.py").read_text(encoding="utf-8")
        self.assertNotIn("def resolve_symbol", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
