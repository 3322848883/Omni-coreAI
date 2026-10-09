# -*- coding: utf-8 -*-
"""T7 触发器按币：槽位按币配额 + yaml 叶条件缺 symbol 不静默。

两条都是「多币种宇宙下静默失效」的形态（设计 §证据附录 B-10 / B-11）：

  B-10 `max_active` 全局共享 → 首币把槽位占满，其余币**永远设不上唤醒条件**
       （弱币系统性出局，且没有任何报错 —— AI 只看到 `trigger_limit`）。
  B-11 yaml 叶条件漏写 `symbol` → `evaluate_condition` 返回 `"no symbol"` →
       **永不触发**，启动时不报错、运行时不告警。

约束：单币路径行为**逐字不变**（设计 I11）—— 单币 bot 的 per-symbol 配额与总上限
同义，再压一层等于把 `max_active: 5` 变成 1，那是能力下降而不是升级。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.trigger_store import (  # noqa: E402
    AITriggerPolicy,
    AITriggerStore,
    TriggerPolicyError,
    validate_trigger_payload,
)
from omnialpha.strategist.triggers import ConditionError, parse_conditions  # noqa: E402

FIVE = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XAU_USDT", "XAG_USDT"]


def _raw(ctype="price_break", symbol="BTC_USDT", **params) -> dict:
    if ctype == "price_break":
        params.setdefault("lookback", 20)
        params.setdefault("side", "low")
    return {"type": ctype, "symbol": symbol, **params}


class TestPerSymbolSlots(unittest.TestCase):
    """B-10：槽位必须按币配额，否则首币占满后其余币永远设不上条件。"""

    def _store(self, td: str, **kw) -> AITriggerStore:
        pol = AITriggerPolicy(enabled=True, **kw)
        return AITriggerStore(Path(td) / "ai_triggers.json", pol)

    def _add(self, store, raw, universe) -> str:
        """返回 '' 表示被拒，否则返回 trigger id。"""
        try:
            norm = validate_trigger_payload(raw, store.policy, universe)
            return store.add(norm, universe).id
        except TriggerPolicyError:
            return ""

    def test_first_symbol_cannot_starve_the_rest(self):
        """5 币宇宙：首币反复 add 只能占自己的配额，其余币仍各有槽位。

        改前：BTC 能把 5 个槽位全部占满 → ETH/SOL/XAU/XAG 的 add 全被
        `trigger_limit` 拒（无任何归因信息）。
        """
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td, max_active=5)
            btc_ok = [
                self._add(s, _raw(lookback=20 + i, side="low"), FIVE)
                for i in range(5)
            ]
            self.assertEqual(
                len([x for x in btc_ok if x]), 1,
                "同币应受 max_active_per_symbol（缺省 1）约束")
            for sym in FIVE[1:]:
                self.assertTrue(
                    self._add(s, _raw(symbol=sym, lookback=20, side="low"), FIVE),
                    f"{sym} 被首币饿死 —— 每个币都该能设上自己的唤醒条件")
            self.assertEqual(sorted(t.symbol for t in s.active()), sorted(FIVE))

    def test_total_cap_still_applies(self):
        """`max_active` 保留为**总上限**：币再多也不能超总量。"""
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td, max_active=3)
            for sym in FIVE[:3]:
                self.assertTrue(self._add(s, _raw(symbol=sym), FIVE))
            with self.assertRaises(TriggerPolicyError) as cm:
                s.add(validate_trigger_payload(_raw(symbol=FIVE[3]), s.policy, FIVE), FIVE)
            self.assertIn("max_active", str(cm.exception))

    def test_per_symbol_cap_configurable(self):
        """配额可配：一币可留 2 个槽位（总上限仍独立生效）。"""
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td, max_active=10, max_active_per_symbol=2)
            uni = ["BTC_USDT", "ETH_USDT"]
            for i in range(2):
                self.assertTrue(self._add(s, _raw(lookback=20 + i), uni))
            self.assertEqual(self._add(s, _raw(lookback=99), uni), "",
                             "超过 max_active_per_symbol 应被拒")
            self.assertTrue(self._add(s, _raw(symbol="ETH_USDT"), uni))

    def test_expired_trigger_frees_its_symbol_slot(self):
        """过期清理与配额**同源**：过期条件不该继续占着该币的槽位。"""
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td, max_active=5)
            self.assertTrue(self._add(s, _raw(), FIVE))
            s._items[0].expire_at = time.time() - 1
            self.assertTrue(self._add(s, _raw(lookback=40), FIVE),
                            "过期条件占着槽位 → 该币再也设不上条件")

    def test_single_symbol_universe_is_not_restricted(self):
        """单币路径逐字不变：单币 bot 的 `max_active: 3` 仍是 3 个槽位。"""
        with tempfile.TemporaryDirectory() as td:
            s = self._store(td, max_active=3)
            for i in range(3):
                self.assertTrue(self._add(s, _raw(lookback=20 + i), ["BTC_USDT"]),
                                "单币 bot 不该被 per-symbol 配额额外收紧")
            self.assertEqual(self._add(s, _raw(lookback=99), ["BTC_USDT"]), "")


class TestSymbolNormalization(unittest.TestCase):
    """白名单与输入两侧都过归一：`BTC` 与 `BTC_USDT` 是同一个币。"""

    def _pol(self, **kw) -> AITriggerPolicy:
        return AITriggerPolicy(enabled=True, **kw)

    def test_short_input_matches_long_whitelist(self):
        out = validate_trigger_payload(_raw(symbol="BTC"), self._pol(),
                                       ["BTC_USDT"])
        self.assertEqual(out["symbol"], "BTC_USDT")

    def test_short_whitelist_matches_long_input(self):
        out = validate_trigger_payload(_raw(symbol="BTC_USDT"),
                                       self._pol(allow_symbols=("BTC",)),
                                       None)
        self.assertEqual(out["symbol"], "BTC_USDT")

    def test_short_bot_symbols_match_long_input(self):
        out = validate_trigger_payload(_raw(symbol="BTC_USDT"), self._pol(),
                                       ["BTC", "ETH"])
        self.assertEqual(out["symbol"], "BTC_USDT")

    def test_lowercase_is_normalized(self):
        out = validate_trigger_payload(_raw(symbol="btc_usdt"), self._pol(),
                                       ["BTC_USDT"])
        self.assertEqual(out["symbol"], "BTC_USDT")

    def test_out_of_universe_still_rejected(self):
        with self.assertRaises(TriggerPolicyError):
            validate_trigger_payload(_raw(symbol="DOGE_USDT"), self._pol(),
                                     ["BTC_USDT", "ETH_USDT"])

    def test_dedup_across_spellings(self):
        """归一后去重语义不变：同一条件的两种写法算同一个（不占第二个槽位）。"""
        with tempfile.TemporaryDirectory() as td:
            s = AITriggerStore(Path(td) / "ai_triggers.json",
                               AITriggerPolicy(enabled=True, max_active=5))
            a = s.add(validate_trigger_payload(_raw(symbol="BTC"), s.policy, FIVE), FIVE)
            b = s.add(validate_trigger_payload(_raw(symbol="BTC_USDT"), s.policy, FIVE), FIVE)
            self.assertEqual(a.id, b.id)
            self.assertEqual(len(s.active()), 1)


class TestYamlConditionSymbol(unittest.TestCase):
    """B-11：yaml 叶条件缺 symbol 不得静默永不触发。"""

    def test_single_symbol_universe_auto_fills(self):
        conds = parse_conditions(
            [{"type": "price_break", "lookback": 20, "side": "low"}],
            symbols=["BTC_USDT"],
        )
        self.assertEqual(conds[0]["symbol"], "BTC_USDT")

    def test_multi_symbol_universe_raises_with_condition_identified(self):
        raw = [
            {"type": "price_break", "symbol": "BTC_USDT", "lookback": 20, "side": "low"},
            {"type": "rsi", "period": 14, "op": "gt", "level": 70},
        ]
        with self.assertRaises(ConditionError) as cm:
            parse_conditions(raw, symbols=["BTC_USDT", "ETH_USDT"])
        msg = str(cm.exception)
        self.assertIn("conditions[1]", msg, "错误必须说清是哪个条件缺 symbol")
        self.assertIn("rsi", msg)
        self.assertIn("symbol", msg)

    def test_nested_child_missing_symbol_raises(self):
        raw = [{"type": "all", "children": [
            {"type": "price_break", "symbol": "BTC_USDT", "lookback": 20, "side": "low"},
            {"type": "rsi", "period": 14, "op": "gt", "level": 70},
        ]}]
        with self.assertRaises(ConditionError) as cm:
            parse_conditions(raw, symbols=["BTC_USDT", "ETH_USDT"])
        self.assertIn("conditions[0].children[1]", str(cm.exception))

    def test_condition_symbol_is_normalized(self):
        conds = parse_conditions(
            [{"type": "price_break", "symbol": "BTC", "lookback": 20, "side": "low"}],
            symbols=["BTC_USDT", "ETH_USDT"],
        )
        self.assertEqual(conds[0]["symbol"], "BTC_USDT")

    def test_no_universe_keeps_legacy_behavior(self):
        """旧调用方（未提供宇宙）行为逐字不变 —— 不能凭空收紧已有调用。"""
        conds = parse_conditions([{"type": "price_break", "lookback": 20, "side": "low"}])
        self.assertEqual(conds[0]["symbol"], "")


if __name__ == "__main__":
    unittest.main()
