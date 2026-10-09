# -*- coding: utf-8 -*-
"""T11：journal / Tier-1 / 近况**按币**（多币不互相污染，单币逐字不变）。

覆盖 spec `symbol-as-parameter.md` §S2.4⑩：

- 多币一轮里每枚币各有自己的动作与 Tier-1（region / invalidation / risk_pct…）。
  原先 journal 只记**不带币名**的动作串（`"hold,open_long"`），Tier-1 只取 `chips[0]`
  —— 其余币的判定凭空消失（审计 D-21/D-22）。
- 近况、决策索引、上轮方案状态都要按币渲染，否则模型看不出「哪个币做了什么」。
- **单币输出逐字不变**（I11）：单币的顶层 Tier-1 字段、`decision`、渲染文本与改动前
  完全一致；`symbols`/`decisions`/`tier1` 只是**增量**字段。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.memory.context import (  # noqa: E402
    _decision_text, _format_index, _format_last_plan_state, _format_recent, build_context,
)
from omnialpha.memory.journal import MemoryJournal, tier1_journal_fields  # noqa: E402


class TestTier1PerSymbol(unittest.TestCase):
    def test_single_chip_flat_fields_unchanged(self):
        """单 chip：顶层字段与改动前**一模一样**（I11），按币字段是增量。"""
        chip = {"symbol": "BTC_USDT", "action": "open_long", "region": "trend",
                "invalidation": 84800, "risk_pct": 0.01, "rule_ids": ["4"],
                "time_stop_bars": 3, "give_back_pct": 40}
        out = tier1_journal_fields([chip])
        for k, v in (("region", "trend"), ("invalidation_price", 84800),
                     ("risk_pct", 0.01), ("rule_ids", ["4"]),
                     ("time_stop_bars", 3), ("give_back_pct", 40)):
            self.assertEqual(out[k], v, k)
        self.assertEqual(out["symbols"], ["BTC_USDT"])
        self.assertEqual(out["decisions"], ["open_long"])
        self.assertEqual(sorted(out["tier1"]), ["BTC_USDT"])

    def test_multi_chip_keeps_every_symbol(self):
        """多币：**每个**币的动作与 Tier-1 都要留下。"""
        chips = [{"symbol": "BTC_USDT", "action": "hold", "region": "range"},
                 {"symbol": "ETH_USDT", "action": "open_long", "invalidation": 2690,
                  "risk_pct": 0.02}]
        out = tier1_journal_fields(chips)
        self.assertEqual(out["symbols"], ["BTC_USDT", "ETH_USDT"])
        self.assertEqual(out["decisions"], ["hold", "open_long"])
        self.assertEqual(out["tier1"]["BTC_USDT"], {"region": "range"})
        self.assertEqual(out["tier1"]["ETH_USDT"],
                         {"invalidation_price": 2690, "risk_pct": 0.02})
        self.assertNotIn("region", out, "多币下顶层字段会歧义 —— 不该写")
        self.assertNotIn("invalidation_price", out)

    def test_empty_and_missing_symbol_are_safe(self):
        self.assertEqual(tier1_journal_fields([]), {})
        out = tier1_journal_fields([{"symbol": "", "action": "hold"}])
        self.assertEqual(out["symbols"], [""])
        self.assertEqual(out["decisions"], ["hold"])
        self.assertNotIn("tier1", out, "没有币就不写 tier1")

    def test_rollback_style_chip_without_tier1_fields(self):
        """chip 什么都不填 → 除按币字段外不产生额外键（老 journal 形态不变）。"""
        out = tier1_journal_fields([{"symbol": "BTC_USDT", "action": "hold"}])
        self.assertEqual(out, {"symbols": ["BTC_USDT"], "decisions": ["hold"]})


class TestRenderPerSymbol(unittest.TestCase):
    def test_decision_text_single_unchanged(self):
        self.assertEqual(_decision_text({"decision": "hold"}), "hold")
        self.assertEqual(
            _decision_text({"decision": "hold", "symbols": ["BTC_USDT"],
                            "decisions": ["hold"]}),
            "hold", "单币必须原样返回 decision（逐字不变）")

    def test_decision_text_multi_carries_symbol(self):
        self.assertEqual(
            _decision_text({"decision": "hold,open_long",
                            "symbols": ["BTC_USDT", "ETH_USDT"],
                            "decisions": ["hold", "open_long"]}),
            "BTC_USDT:hold ETH_USDT:open_long")

    def test_recent_and_index_use_per_symbol_text(self):
        rows = [{"cycle_id": "c1", "decision": "hold,open_long",
                 "symbols": ["BTC_USDT", "ETH_USDT"],
                 "decisions": ["hold", "open_long"], "reasoning": "r"}]
        self.assertIn("c1: BTC_USDT:hold ETH_USDT:open_long", _format_recent(rows))
        self.assertIn("c1 BTC_USDT:hold ETH_USDT:open_long", _format_index(rows))

    def test_recent_single_unchanged(self):
        rows = [{"cycle_id": "c1", "decision": "open_long", "reasoning": "因为…",
                 "symbols": ["BTC_USDT"], "decisions": ["open_long"]}]
        self.assertEqual(_format_recent(rows), "c1: open_long — 因为…")
        self.assertEqual(_format_index(rows),
                         "\n[近期决策索引·最近 1 轮（要看某轮细节用 journal_lookup(cycle_id)）]\n"
                         "  c1 open_long")

    def test_last_plan_state_single_unchanged(self):
        r = {"region": "trend", "invalidation_price": 84800, "time_stop_bars": 3}
        self.assertEqual(
            _format_last_plan_state([r]),
            "区域=trend；前提失效=84800（触及即视为结构破坏，须撤单或离场）；最大持仓=3 轮")

    def test_last_plan_state_multi_per_symbol(self):
        r = {"tier1": {"BTC_USDT": {"region": "range"},
                       "ETH_USDT": {"invalidation_price": 2690}}}
        out = _format_last_plan_state([r])
        self.assertIn("BTC_USDT: 区域=range", out)
        self.assertIn("ETH_USDT: 前提失效=2690", out)


class TestJournalEndToEnd(unittest.TestCase):
    def test_two_symbols_are_recoverable_from_one_record(self):
        """两币一轮 → 一条 journal 记录能还原出**两个币各自的**决策与 Tier-1。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            j = MemoryJournal(root, "b1")
            chips = [{"symbol": "BTC_USDT", "action": "hold", "region": "range"},
                     {"symbol": "ETH_USDT", "action": "open_long", "invalidation": 2690}]
            acts = ",".join(c["action"] for c in chips)
            j.append(cycle_id="c1", decision=acts, reasoning="r",
                     **tier1_journal_fields(chips))
            rec = j.read_recent(1)[0]
            self.assertEqual(rec["symbols"], ["BTC_USDT", "ETH_USDT"])
            self.assertEqual(rec["decisions"], ["hold", "open_long"])
            self.assertEqual(rec["tier1"]["ETH_USDT"]["invalidation_price"], 2690)
            # 老字段照旧（下游按 .get() 读，不会因为多了键而改行为）
            self.assertEqual(rec["decision"], "hold,open_long")

    def test_build_context_renders_both_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            MemoryJournal(root, "b1").append(
                cycle_id="c1", decision="hold,open_long", reasoning="理由",
                **tier1_journal_fields([
                    {"symbol": "BTC_USDT", "action": "hold", "region": "range"},
                    {"symbol": "ETH_USDT", "action": "open_long", "invalidation": 2690}]))

            ctx = build_context(root, "b1", system_prompt="SYS", n_recent=2, n_index=5)
            self.assertIn("BTC_USDT:hold", ctx["user"])
            self.assertIn("ETH_USDT:open_long", ctx["user"])
            self.assertIn("BTC_USDT: 区域=range", ctx["user"])
            self.assertIn("ETH_USDT: 前提失效=2690", ctx["user"])

    def test_build_context_single_symbol_unchanged(self):
        """单币：近况/索引/上轮状态三块与改动前逐字相同（I11 golden）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            MemoryJournal(root, "b1").append(
                cycle_id="c1", decision="open_long", reasoning="理由",
                **tier1_journal_fields([
                    {"symbol": "BTC_USDT", "action": "open_long", "region": "trend",
                     "invalidation": 84800}]))

            ctx = build_context(root, "b1", system_prompt="SYS", n_recent=2, n_index=5)
            self.assertIn("c1: open_long", ctx["user"])
            self.assertIn("c1 open_long", ctx["user"])
            self.assertIn("区域=trend；前提失效=84800", ctx["user"])
            self.assertNotIn("BTC_USDT:", ctx["user"], "单币不该出现按币前缀")


if __name__ == "__main__":
    unittest.main()
