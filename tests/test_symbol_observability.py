# -*- coding: utf-8 -*-
"""symbol 维度的观测：`tool_usage` 清零 / 按币统计 / 图元数据落盘（spec T10 / S2.4⑨）。

回归背景（D-24 / D-26 / D-25 / D-27）：
- `run_once` 不清 `tool_usage` → plan-loop **跨轮累加**，`tool_usage_summary` 统计失真；
- summary 只按工具名 → 算不出「缺 symbol 率 / 越界率 / 每币取数次数」；
- 工具调用审计体里没有 symbol → 事后无法按币归因「模型是不是拿错币了」；
- `thinking.json` 没有 charts 字段 → 「这轮给模型发了哪几个币哪些周期的图」不可核对。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from omnialpha.strategist.loop import PlanRunner, StrategistConfig

BOT = "obs-bot"

HOLD_PLAN = json.dumps({
    "chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.9}],
})


class _Client:
    """最小交易所客户端（不联网；返回占位数据，只为让 collect_snapshot 跑通）。"""

    def public_get(self, path, qs=""):
        return [[1000, "1", "1", "1", "1", "1", "0"]]

    def get_ticker(self, sym):
        return {"last": "84000"}

    def get_last_price(self, sym):
        return 84000.0

    def get_contract(self, sym):
        return SimpleNamespace(quanto_multiplier=0.0001, order_size_round=0,
                               order_price_round=0.1, leverage_max=20)

    def get_contract_stats(self, sym, limit=1):
        return []

    def get_orderbook_top(self, sym, limit=5):
        return {"bids": [], "asks": []}

    def get_account(self):
        return {}

    def get_positions(self):
        return []

    def list_orders(self):
        return []

    def list_price_orders(self, sym):
        return []


class _ToolThenPlanLLM:
    """奇数调用返回 tool_calls、偶数调用返回 hold plan（legacy 文本工具路径）。"""

    def __init__(self):
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        if self.calls % 2 == 1:
            return json.dumps({"tool_calls": [
                {"tool": "klines", "args": {"symbol": "BTC_USDT", "tf": "15m"}},
            ]})
        return HOLD_PLAN


def _plan_runner(root: Path, llm, tools=None):
    cfg = StrategistConfig(
        symbols=["BTC_USDT"], timeframe="15m", candles=5, env="testnet",
        write_hold=True, vision=False, prompt_file="",
        tools=tools or {}, bot_root=root, bot_id=BOT,
    )
    return PlanRunner(_Client(), cfg, root / "inbox" / BOT, root / "hist" / BOT, llm=llm)


class TestRunOnceClearsToolUsage(unittest.TestCase):
    """(c) 同一轮内两次 `run_once` 的 `tool_usage` **不累加**。"""

    def test_not_accumulated_across_run_once(self):
        from omnialpha.strategist import loop as loop_mod

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            llm = _ToolThenPlanLLM()
            runner = _plan_runner(root, llm, tools={"enabled": True, "native": False,
                                                    "max_rounds": 1})
            orig = loop_mod.run_tool
            loop_mod.run_tool = lambda *a, **kw: {"ok": True}
            try:
                runner.run_once()
                self.assertEqual(len(runner.tool_usage), 1)
                runner.run_once()
                self.assertEqual(len(runner.tool_usage), 1,
                                 "tool_usage 跨轮累加了（run_once 没清零）")
                self.assertEqual(runner._tool_usage_summary()["counts"], {"klines": 1})
                self.assertEqual(runner._tool_usage_summary()["total_calls"], 1)
            finally:
                loop_mod.run_tool = orig


class TestToolUsageSummaryBySymbol(unittest.TestCase):
    """(d) 有/无 symbol 混合调用下的 `by_symbol` / `missing_symbol` / `out_of_universe`。"""

    def _runner(self, symbols=("BTC_USDT", "ETH_USDT")):
        runner = PlanRunner.__new__(PlanRunner)
        runner.cfg = SimpleNamespace(symbols=list(symbols))
        runner.tool_usage = []
        return runner

    def test_summary_counts_mixed_calls(self):
        r = self._runner()
        # 显式给了、且各自在宇宙内
        r._record_tool_use("klines", {"symbol": "BTC_USDT", "tf": "1h"}, {"ok": 1},
                           raw_args={"symbol": "BTC_USDT", "tf": "1h"})
        r._record_tool_use("ticker", {"symbol": "ETH_USDT"}, {"ok": 1},
                           raw_args={"symbol": "ETH_USDT"})
        r._record_tool_use("ticker", {"symbol": "BTC_USDT"}, {"ok": 1},
                           raw_args={"symbol": "BTC_USDT"})
        # 多币宇宙漏写 symbol → 拒绝（`ambiguous_multi`），**不进 by_symbol**
        r._record_tool_use("indicators", {}, {"error": "symbol_required"}, raw_args={})
        # 显式写了、且不在宇宙内的币（`out_of_universe`）
        r._record_tool_use("klines", {"symbol": "SOL_USDT"}, {"error": "x"},
                           raw_args={"symbol": "SOL_USDT"})
        # 与币无关的工具：不进 symbol 维度
        r._record_tool_use("skill", {"name": "pa-analysis"}, {"content": "x"})

        s = r._tool_usage_summary()
        # 旧字段逐字不变
        self.assertEqual(s["counts"],
                         {"klines": 2, "ticker": 2, "indicators": 1, "skill": 1})
        self.assertEqual(s["total_calls"], 6)
        self.assertTrue(s["data_checked"])
        # 新字段
        self.assertEqual(s["by_symbol"]["BTC_USDT"],
                         {"calls": 2, "tools": ["klines", "ticker"]})
        self.assertEqual(s["by_symbol"]["ETH_USDT"], {"calls": 1, "tools": ["ticker"]})
        self.assertNotIn("SOL_USDT", s["by_symbol"], "越界 symbol 不该进 by_symbol")
        self.assertEqual(s["missing_symbol"], 1, "多币宇宙漏写 symbol → ambiguous_multi")
        self.assertEqual(s["out_of_universe"], 1)

    def test_single_symbol_universe_auto_fill_is_counted_as_missing(self):
        """单币下模型漏写 symbol 仍计 `missing_symbol`（币一多就会变成拒绝，最早的预警）。"""
        r = self._runner(symbols=("BTC_USDT",))
        r._record_tool_use("klines", {"symbol": "BTC_USDT"}, {"ok": 1}, raw_args={})
        s = r._tool_usage_summary()
        self.assertEqual(s["by_symbol"], {"BTC_USDT": {"calls": 1, "tools": ["klines"]}})
        self.assertEqual(s["missing_symbol"], 1)

    def test_audit_body_carries_symbol(self):
        """(5) 工具调用审计体带 symbol（多币下事后按币归因的唯一依据）。"""
        r = self._runner()
        r._record_tool_use("klines", {"symbol": "ETH_USDT", "tf": "4h"}, {"ok": 1},
                           raw_args={"symbol": "ETH_USDT", "tf": "4h"})
        entry = r.tool_usage[0]
        self.assertEqual(entry["symbol"], "ETH_USDT")
        self.assertEqual(entry["symbol_note"], "explicit")

    def test_non_symbol_tool_has_no_symbol_fields(self):
        r = self._runner()
        r._record_tool_use("journal_lookup", {"cycle_id": "c1"}, {"ok": 1})
        self.assertNotIn("symbol", r.tool_usage[0])
        self.assertNotIn("symbol_note", r.tool_usage[0])


class TestChartsMetadataInThinking(unittest.TestCase):
    """(e) `thinking.json` 出现 charts 元数据，且**不含 base64 本体**。"""

    def _runner(self, symbols, tfs, timeframe="15m"):
        runner = PlanRunner.__new__(PlanRunner)
        runner.cfg = SimpleNamespace(symbols=list(symbols), timeframe=timeframe,
                                     vision_timeframes=list(tfs))
        runner.tool_usage = []
        return runner

    @staticmethod
    def _rows(base):
        return [{"t": 1790900000 + i * 900, "o": base + i, "h": base + 100 + i,
                 "l": base - 100 + i, "c": base + 50 + i, "v": 10 + i} for i in range(20)]

    def test_charts_metadata_written_without_base64_body(self):
        from omnialpha.strategist import vision

        with tempfile.TemporaryDirectory() as td:
            runner = self._runner(["BTC_USDT", "ETH_USDT"], ["15m", "1h"])
            runner.history_dir = Path(td) / "state"
            body = "data:image/png;base64," + "A" * 4000
            orig = vision.generate_and_encode
            vision.generate_and_encode = lambda k, symbol="", timeframe="": body
            try:
                charts = runner._generate_charts({
                    "market": {"BTC_USDT": {"candles": self._rows(84000),
                                            "tf": {"1h": {"candles": self._rows(84000)}}},
                               "ETH_USDT": {"candles": self._rows(2700),
                                            "tf": {"1h": {"candles": self._rows(2700)}}}}})
            finally:
                vision.generate_and_encode = orig
            self.assertEqual(len(charts), 4, "2 币 × 2 周期")

            runner._save_thinking(cycle_id="c-1", trigger="interval",
                                  reasoning=["r"], content=HOLD_PLAN)
            files = list(runner.history_dir.glob("*.thinking.json"))
            self.assertEqual(len(files), 1)
            raw = files[0].read_text(encoding="utf-8")
            rec = json.loads(raw)
            self.assertEqual([c["symbol"] for c in rec["charts"]],
                             ["BTC_USDT", "BTC_USDT", "ETH_USDT", "ETH_USDT"])
            self.assertEqual([c["timeframe"] for c in rec["charts"]],
                             ["15m", "1h", "15m", "1h"])
            self.assertTrue(all(c["ok"] for c in rec["charts"]))
            self.assertEqual(rec["charts"][0]["bytes"], len(body))
            self.assertNotIn("A" * 100, raw, "base64 本体被写进了 thinking.json")

    def test_failed_chart_is_recorded(self):
        """没生成成功的图也要留痕（否则「没图」与「没跑」分不清）。"""
        runner = self._runner(["BTC_USDT"], ["15m"])
        charts = runner._generate_charts({"market": {"BTC_USDT": {}}})
        self.assertEqual(charts, [])
        self.assertEqual(runner._last_chart_meta,
                         [{"symbol": "BTC_USDT", "timeframe": "15m", "ok": False,
                           "reason": "no_candles"}])

    def test_no_vision_means_empty_charts_field(self):
        """vision 关掉时 `charts` 是空列表（不是缺字段，也不会挂着上一轮的图）。"""
        with tempfile.TemporaryDirectory() as td:
            runner = self._runner(["BTC_USDT"], ["15m"])
            runner.history_dir = Path(td) / "state"
            runner._last_chart_meta = [{"symbol": "STALE_USDT", "timeframe": "1h"}]
            runner._last_chart_meta = []  # run_once/analyze_once 入口就是这么清的
            runner._save_thinking(cycle_id="c-2", trigger="interval",
                                  reasoning=[], content=HOLD_PLAN)
            rec = json.loads(next(runner.history_dir.glob("*.thinking.json"))
                             .read_text(encoding="utf-8"))
            self.assertEqual(rec["charts"], [])
            # 旧字段仍在（只新增、不删改）
            for key in ("ts", "cycle_id", "trigger", "reasoning_chain", "content_head",
                        "tool_usage", "tool_usage_summary"):
                self.assertIn(key, rec)


if __name__ == "__main__":
    unittest.main()
