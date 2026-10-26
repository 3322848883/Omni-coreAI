# -*- coding: utf-8 -*-
"""取数覆盖度闸门（`PlanRunner._data_coverage` + `_chat_with_tools` 重试）。

背景（2026-10-26 实测 martin-grid-paper 4 轮）：人格第八节要求
「5 个周期 × (`smc_map` + `smc_events`) = 10 次调用」，模型实际只调 4 次
（`smc_map` 15m/1h/4h + `smc_events` 15m）。原来的防偷懒只有一条
「**一次都没调**才重试」，而 `data_checked = bool(交集非空)` —— 调 1 次就算过。
于是模型从来不看日线，却宣称「马丁第 3 档落在日线结构位」。

本文件钉住：缺哪个周期就点名，重试一次且只重试一次。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.strategist.loop import PlanRunner, StrategistConfig

REQ = {"smc_map": ["15m", "1h", "4h", "1d"], "smc_events": ["15m", "1h"]}


def _runner(td: str, tools: dict | None = None) -> PlanRunner:
    cfg = StrategistConfig(
        symbols=["ETH_USDT"],
        vision=False,
        prompt_file="",
        bot_root=Path(td),
        bot_id="dc",
        env="paper",
        write_hold=True,
        tools=tools if tools is not None else {"enabled": True},
    )
    return PlanRunner(client=object(), cfg=cfg,
                      inbox=Path(td) / "inbox", history_dir=Path(td) / "state",
                      llm=object())


class TestCoverage(unittest.TestCase):
    def _use(self, runner, *calls):
        """calls: (tool, tf)"""
        runner.tool_usage = [{"tool": t, "args": {"symbol": "ETH_USDT", "tf": tf}}
                             for t, tf in calls]

    def test_not_configured_means_no_check(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)
            self._use(r, ("smc_map", "15m"))
            _, missing = r._data_coverage()
            self.assertEqual(missing, [], "没配 required_coverage 就不该拦")

    def test_full_coverage_is_clean(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, {"enabled": True, "required_coverage": REQ})
            self._use(r, ("smc_map", "15m"), ("smc_map", "1h"), ("smc_map", "4h"),
                      ("smc_map", "1d"), ("smc_events", "15m"), ("smc_events", "1h"))
            covered, missing = r._data_coverage()
            self.assertEqual(missing, [])
            self.assertEqual(covered["smc_map"], {"15m", "1h", "4h", "1d"})

    def test_missing_day_and_events_are_named(self):
        """真实现场：只查了 map 15m/1h/4h + events 15m。"""
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, {"enabled": True, "required_coverage": REQ})
            self._use(r, ("smc_map", "15m"), ("smc_map", "1h"), ("smc_map", "4h"),
                      ("smc_events", "15m"))
            _, missing = r._data_coverage()
            self.assertIn("smc_map:1d", missing, f"必须点名日线：{missing}")
            self.assertIn("smc_events:1h", missing)
            self.assertEqual(len(missing), 2)

    def test_case_and_space_normalised(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, {"enabled": True, "required_coverage": {"smc_map": ["1D"]}})
            self._use(r, ("smc_map", " 1d "))
            _, missing = r._data_coverage()
            self.assertEqual(missing, [])

    def test_meta_tool_without_tf_not_counted(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, {"enabled": True, "required_coverage": {"smc_map": ["1d"]}})
            r.tool_usage = [{"tool": "skill", "args": {"name": "x"}},
                            {"tool": "klines", "args": {"symbol": "ETH_USDT"}}]
            _, missing = r._data_coverage()
            self.assertEqual(missing, ["smc_map:1d"], "没有 tf 的调用不该充数")


class TestSummaryShape(unittest.TestCase):
    """`coverage_missing` 只在**配了** required_coverage 时出现。

    `tests/test_symbol_observability.py::test_old_fields_shape_unchanged` 钉住了
    旧字段集合 —— 给所有 bot 无条件加键会破坏那个契约（实测 6 个 error + 1 fail）。
    """

    def test_absent_when_not_configured(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)                      # 没配 required_coverage
            r.tool_usage = [{"tool": "smc_map", "args": {"tf": "15m"}}]
            s = r._tool_usage_summary()
            self.assertNotIn("coverage_missing", s, f"未配置时形状必须不变：{sorted(s)}")
            self.assertEqual(set(s), {"counts", "total_calls", "data_checked",
                                      "by_symbol", "missing_symbol", "out_of_universe"})

    def test_present_and_accurate_when_configured(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, {"enabled": True, "required_coverage": REQ})
            r.tool_usage = [{"tool": "smc_map", "args": {"tf": "15m"}}]
            s = r._tool_usage_summary()
            self.assertIn("coverage_missing", s)
            self.assertIn("smc_map:1d", s["coverage_missing"])
            self.assertIn("smc_events:1h", s["coverage_missing"])

    def test_missing_tools_cfg_does_not_crash(self):
        """`cfg` 没有 tools 属性（旧 cfg / 测试替身）时不许炸。"""
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)
            del r.cfg.tools
            r.tool_usage = [{"tool": "smc_map", "args": {"tf": "15m"}}]
            s = r._tool_usage_summary()
            self.assertNotIn("coverage_missing", s)


class TestRetry(unittest.TestCase):
    def _patch_inner(self, runner, first, second, seen: list):
        def inner(system, user, chart_base64=None):
            seen.append(user)
            return second if len(seen) > 1 else first
        runner._chat_with_tools_inner = inner

    def test_missing_triggers_one_retry_with_named_items(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, {"enabled": True, "required_coverage": REQ})
            r.tool_usage = [{"tool": "smc_map", "args": {"tf": "15m"}},
                            {"tool": "smc_map", "args": {"tf": "1h"}},
                            {"tool": "smc_map", "args": {"tf": "4h"}},
                            {"tool": "smc_events", "args": {"tf": "15m"}}]
            seen: list[str] = []
            self._patch_inner(r, "first-plan", "second-plan", seen)
            out = r._chat_with_tools("sys", "usr")
            self.assertEqual(out, "second-plan", "缺数据时要用重试后的输出")
            self.assertEqual(len(seen), 2, "只重试一次")
            self.assertIn("smc_map:1d", seen[1], "必须把缺失项点名给模型")
            self.assertIn("smc_events:1h", seen[1])

    def test_no_retry_when_complete(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, {"enabled": True, "required_coverage": {"smc_map": ["1d"]}})
            r.tool_usage = [{"tool": "smc_map", "args": {"tf": "1d"}}]
            seen: list[str] = []
            self._patch_inner(r, "only", "never", seen)
            self.assertEqual(r._chat_with_tools("sys", "usr"), "only")
            self.assertEqual(len(seen), 1, "覆盖齐全不该多花一次 LLM 调用")

    def test_no_retry_when_not_configured(self):
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td)                    # tools.enabled=True，但没配覆盖表
            r.tool_usage = [{"tool": "smc_map", "args": {"tf": "15m"}}]
            seen: list[str] = []
            self._patch_inner(r, "only", "never", seen)
            self.assertEqual(r._chat_with_tools("sys", "usr"), "only")
            self.assertEqual(len(seen), 1)

    def test_empty_second_answer_falls_back(self):
        """重试拿回空串时，不能把整轮变成空计划 —— 退回第一次的输出。"""
        with tempfile.TemporaryDirectory() as td:
            r = _runner(td, {"enabled": True, "required_coverage": {"smc_map": ["1d"]}})
            r.tool_usage = [{"tool": "smc_map", "args": {"tf": "15m"}}]
            seen: list[str] = []
            self._patch_inner(r, "first-plan", "   ", seen)
            self.assertEqual(r._chat_with_tools("sys", "usr"), "first-plan")


if __name__ == "__main__":
    unittest.main()
