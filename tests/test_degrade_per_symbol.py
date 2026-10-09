# -*- coding: utf-8 -*-
"""降级 / 讨论 / 兜底都不得再"猜首币"（T3；证据 B-13/B-14/B-15、D-21）。

回归背景（2026-10-09 全量审计）：
- `_hold_plan` / `_hold_fallback` 只给 `symbols[0]` 一条 hold，且宇宙为空时写死 `BTC_USDT`
  —— 多币下其余币在 journal/近况里**完全缺席**（等于被静默丢出决策）；
- 讨论 chip 的 symbol 兜底链以 `"BTC_USDT"` 收尾，且越界会**静默改成** `allowed[0]`
  —— 模型本意 SOL 的结论会被执行成 ETH；
- `_resolve_order_id` 用 `opens[0]`（文件名字典序）复用/关闭订单 —— 多币组里可能
  关错币的单。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.persona.runner import PersonaRunner  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402

TWO = ["BTC_USDT", "ETH_USDT"]


def _runner(symbols) -> PlanRunner:
    r = PlanRunner.__new__(PlanRunner)
    r.cfg = StrategistConfig(symbols=list(symbols))
    return r


class _Orders:
    def __init__(self, rows):
        self.rows = list(rows)
        self.updated = []

    def list_open(self, group=None):
        return [r for r in self.rows if group is None or r.get("group") == group]

    def update(self, order_id, **kw):
        self.updated.append((order_id, kw))
        return True


class _Group:
    name = "g1"
    members = ["bot-a"]


class TestDegradePerSymbol(unittest.TestCase):
    def test_hold_plan_covers_whole_universe(self):
        plan = _runner(TWO)._hold_plan("c1", "llm_failed")
        self.assertEqual([c.symbol for c in plan.chips], TWO,
                         "降级必须逐币给 hold，否则其余币在 journal 里完全缺席")
        self.assertTrue(all(c.action == "hold" for c in plan.chips))

    def test_hold_plan_never_invents_btc(self):
        """宇宙为空 → 不许写 `BTC_USDT`（那是在猜一个本 bot 没配的标的）。"""
        plan = _runner([])._hold_plan("c1", "llm_failed")
        self.assertEqual([c.symbol for c in plan.chips], [""])
        self.assertNotIn("BTC_USDT", [c.symbol for c in plan.chips])

    def test_hold_fallback_covers_whole_universe(self):
        res = _runner(TWO)._hold_fallback("c1", "kline_close", "parse_failed")
        syms = [c["symbol"] for c in res["plan"]["chips"]]
        self.assertEqual(syms, TWO)

    def test_hold_fallback_never_invents_btc(self):
        res = _runner([])._hold_fallback("c1", "interval", "llm_failed")
        self.assertNotIn("BTC_USDT", [c["symbol"] for c in res["plan"]["chips"]])

    def test_default_symbol_only_when_unique(self):
        self.assertEqual(_runner(["ETH_USDT"])._default_symbol(), "ETH_USDT")
        self.assertEqual(_runner(TWO)._default_symbol(), "",
                         "多币宇宙里'首币'不是唯一解，不该补")

    def test_prompt_risk_carries_universe_and_quota(self):
        """风控段必须告诉模型宇宙与**每币名额** —— 否则它会以为几条 chip 能落在同一个币上。"""
        risk = _runner(TWO)._prompt_risk({"account": {"total": 1000}})
        self.assertEqual(risk["symbols"], TWO)
        self.assertIn("max_chips_per_symbol", risk)


class TestDiscussionChipSymbol(unittest.TestCase):
    def _chip(self, plan_symbol, decision="open_long", allowed=TWO, default=""):
        plan = {"chips": [{"symbol": plan_symbol, "action": "hold",
                           "size_usd": 100.0, "price": 1.0, "sl": 0.5}]}
        return PlanRunner._discussion_chip(
            {"sl": 0.5, "price": 1.0}, decision, plan,
            default_symbol=default, allowed=allowed)

    def test_in_universe_ok(self):
        chip = self._chip("ETH_USDT")
        self.assertIsNotNone(chip)
        self.assertEqual(chip["symbol"], "ETH_USDT")

    def test_out_of_universe_rejected_not_corrected(self):
        """越界 → 拒绝（None），不得静默改成 `allowed[0]`。"""
        self.assertIsNone(self._chip("SOL_USDT"))

    def test_missing_symbol_multi_rejected(self):
        self.assertIsNone(self._chip("", default=""))

    def test_missing_symbol_single_filled(self):
        """单币宇宙补它是唯一解，无歧义 —— 保持可用（不是降级）。"""
        chip = self._chip("", allowed=["ETH_USDT"], default="ETH_USDT")
        self.assertIsNotNone(chip)
        self.assertEqual(chip["symbol"], "ETH_USDT")


class TestResolveOrderIdBySymbol(unittest.TestCase):
    def _runner_with(self, rows):
        r = PersonaRunner.__new__(PersonaRunner)
        r.group = _Group()
        r.orders = _Orders(rows)
        return r

    def test_reuses_matching_symbol_only(self):
        """多币组：ETH 的单存在、BTC 的单更新 —— 按币取，不能取"字典序第一张"。"""
        rows = [
            {"order_id": "o-btc", "group": "g1", "symbol": "BTC_USDT", "side": "long"},
            {"order_id": "o-eth", "group": "g1", "symbol": "ETH_USDT", "side": "long"},
        ]
        r = self._runner_with(rows)
        self.assertEqual(r._resolve_order_id("hold", {}, symbol="ETH_USDT"), "o-eth")
        self.assertEqual(r._resolve_order_id("hold", {}, symbol="BTC_USDT"), "o-btc")

    def test_reversal_closes_matching_symbol_only(self):
        rows = [
            {"order_id": "o-btc", "group": "g1", "symbol": "BTC_USDT", "side": "long"},
            {"order_id": "o-eth", "group": "g1", "symbol": "ETH_USDT", "side": "long"},
        ]
        r = self._runner_with(rows)
        r._resolve_order_id("short", {}, symbol="ETH_USDT")
        self.assertEqual([x[0] for x in r.orders.updated], ["o-eth"],
                         "方向反转只能关掉**同一个币**的那张单")

    def test_no_filter_when_symbol_unknown(self):
        """全组未配 symbols（symbol=""）→ 退化为旧行为，不做过滤。"""
        rows = [{"order_id": "o-x", "group": "g1", "symbol": "", "side": "long"}]
        r = self._runner_with(rows)
        self.assertEqual(r._resolve_order_id("hold", {}, symbol=""), "o-x")


class TestExecuteSymbolPolicy(unittest.TestCase):
    """`_execute` 的标的策略：唯一解纠正（旧能力），多币歧义拒绝（新语义）。"""

    def _run(self, allowed_symbols, chip_symbol):
        import json
        import tempfile
        from types import SimpleNamespace

        class _Bot:
            symbols = list(allowed_symbols)
            strategist = {}

        with tempfile.TemporaryDirectory() as td:
            r = PersonaRunner(Path(td), _Group(), {"bot-a": _Bot()}, {})
            r.group = SimpleNamespace(name="g1", members=["bot-a"],
                                      target_account="bot-a", topology="single_account",
                                      fusion_config={})
            fusion = {"decision": "long", "action": "open_long", "confidence": 0.7,
                      "votes": {"bot-a": "long"}, "mode": "weighted_vote"}
            plans = {"bot-a": {"chips": [{"symbol": chip_symbol, "action": "open_long",
                                          "sl": 1.0, "price": 2.0, "size_usd": 100.0}]}}
            res = r._execute(fusion, plans, None)
            return json.loads(Path(res["signal_file"]).read_text(encoding="utf-8"))

    def test_unique_symbol_is_corrected_with_trace(self):
        sig = self._run(["ETH_USDT"], "BTC_USDT")
        self.assertEqual(sig["symbol"], "ETH_USDT")
        self.assertEqual(sig["meta"]["symbol_corrected"], "BTC_USDT→ETH_USDT")

    def test_multi_symbol_out_of_universe_is_rejected(self):
        """多币宇宙里越界 → 降成 hold 并留痕，**不静默改成首币**。"""
        sig = self._run(["ETH_USDT", "SOL_USDT"], "BTC_USDT")
        self.assertEqual(sig["action"], "hold")
        self.assertEqual(sig["meta"]["symbol_rejected"], "BTC_USDT")
        self.assertEqual(sig["meta"]["rejected_action"], "open_long")


if __name__ == "__main__":
    unittest.main()
