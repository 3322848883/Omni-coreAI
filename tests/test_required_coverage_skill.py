# -*- coding: utf-8 -*-
"""`tools.required_coverage` 的覆盖面检查 —— 含 `skill` 必查项。

2026-10-28 需求（用户）：**必须先加载技能、并分析四周期行情，才能出方案**。

原实现只统计「工具 × 周期」（`tool_usage` 里 tool+args.tf），因此：
  · `skill` 无法作为必查项 → 「先载技能再下结论」只是软约束，模型可以整轮跳过；
  · 补数据提示把 `smc_map`/`smc_events`/日线/马丁写死在字符串里 → 对 PA 类 bot 是噪音。

本文件钉住：
  1. `skill: [名字]` 能作为必查项，缺失时进 `missing`；
  2. 周期项行为不变（`indicators: [5m, 15m, 1h, 4h]`）；
  3. 提示文本按缺失项动态生成，skill 与周期分开说明，且**不含别的 bot 的术语**；
  4. 未配置 `required_coverage` 的 bot 输出形状不变。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.loop import PlanRunner  # noqa: E402


def _mk(required=None, usage=None) -> PlanRunner:
    """构造一个只带 coverage 相关字段的 PlanRunner 壳（不连交易所/LLM）。"""
    obj = object.__new__(PlanRunner)
    tools = {"enabled": True}
    if required is not None:
        tools["required_coverage"] = required
    obj.cfg = SimpleNamespace(tools=tools)
    obj.tool_usage = list(usage or [])
    return obj


class TestSkillCoverage(unittest.TestCase):
    def test_skill_missing_is_reported(self):
        s = _mk({"skill": ["brooks-bar-by-bar"]}, usage=[
            {"tool": "indicators", "args": {"tf": "5m"}},
        ])
        _, missing = s._data_coverage()
        self.assertEqual(missing, ["skill:brooks-bar-by-bar"])

    def test_skill_present_clears(self):
        s = _mk({"skill": ["brooks-bar-by-bar"]}, usage=[
            {"tool": "skill", "args": {"name": "brooks-bar-by-bar"}},
        ])
        _, missing = s._data_coverage()
        self.assertEqual(missing, [])

    def test_wrong_skill_does_not_count(self):
        s = _mk({"skill": ["brooks-bar-by-bar"]}, usage=[
            {"tool": "skill", "args": {"name": "smc-deep"}},
        ])
        _, missing = s._data_coverage()
        self.assertEqual(missing, ["skill:brooks-bar-by-bar"])


class TestTimeframeCoverage(unittest.TestCase):
    CFG = {"indicators": ["5m", "15m", "1h", "4h"]}

    def test_all_four_timeframes_covered(self):
        usage = [{"tool": "indicators", "args": {"tf": tf}}
                 for tf in ("5m", "15m", "1h", "4h")]
        s = _mk(self.CFG, usage=usage)
        _, missing = s._data_coverage()
        self.assertEqual(missing, [])

    def test_partial_timeframes_reported_in_order(self):
        usage = [{"tool": "indicators", "args": {"tf": "5m"}},
                 {"tool": "indicators", "args": {"tf": "1h"}}]
        s = _mk(self.CFG, usage=usage)
        _, missing = s._data_coverage()
        self.assertEqual(missing, ["indicators:15m", "indicators:4h"])

    def test_skill_call_does_not_fake_timeframe(self):
        """skill 调用没有 tf，不能顶替周期项（回归：曾把它当无键条目）。"""
        s = _mk(self.CFG, usage=[
            {"tool": "skill", "args": {"name": "brooks-bar-by-bar"}},
        ])
        _, missing = s._data_coverage()
        self.assertEqual(len(missing), 4)


class TestNudgeText(unittest.TestCase):
    def test_skill_and_data_split(self):
        s = _mk({"skill": ["brooks-bar-by-bar"], "indicators": ["15m", "4h"]})
        txt = s._coverage_nudge(["skill:brooks-bar-by-bar",
                                 "indicators:15m", "indicators:4h"])
        self.assertIn("先调用 `skill` 加载：brooks-bar-by-bar", txt)
        self.assertIn("indicators:15m", txt)
        self.assertIn("indicators:4h", txt)

    def test_no_foreign_bot_terminology(self):
        """提示里不许出现别的 bot 的术语（smc/马丁），否则对 PA 是纯噪音。"""
        s = _mk({"skill": ["brooks-bar-by-bar"]})
        txt = s._coverage_nudge(["skill:brooks-bar-by-bar"])
        for bad in ("smc_map", "smc_events", "马丁", "日线结构"):
            self.assertNotIn(bad, txt, f"提示里不该出现 {bad}")


class TestUnconfiguredShape(unittest.TestCase):
    def test_no_required_coverage_has_no_missing(self):
        s = _mk(None, usage=[{"tool": "indicators", "args": {"tf": "5m"}}])
        _, missing = s._data_coverage()
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()


class TestTwoStageRetryIsSequential(unittest.TestCase):
    """两级防偷懒必须**串行**（2026-10-28 实测 bug：第 1 级重试后直接 return，
    覆盖检查被整段跳过 → 只查了 5m 也照样出单）。"""

    class _R(PlanRunner):
        def __init__(self, usage_seq):
            self.cfg = SimpleNamespace(tools={
                "enabled": True,
                "required_coverage": {"skill": ["brooks-bar-by-bar"],
                                      "indicators": ["5m", "15m", "1h", "4h"]},
            })
            self.tool_usage = []
            self._seq = list(usage_seq)
            self.prompts: list[str] = []

        def _chat_with_tools_inner(self, system, user, chart_base64=None):
            self.prompts.append(user)
            self.tool_usage = self._seq.pop(0) if self._seq else []
            return "PLAN"

    def test_skill_only_then_partial_still_triggers_coverage_retry(self):
        r = self._R([
            [{"tool": "skill", "args": {"name": "brooks-bar-by-bar"}}],          # 只调 skill
            [{"tool": "ticker", "args": {"symbol": "ETH_USDT"}},                 # 被逼补数
             {"tool": "indicators", "args": {"tf": "5m"}}],                      # 仍只 5m
            [{"tool": "indicators", "args": {"tf": "15m"}},
             {"tool": "indicators", "args": {"tf": "1h"}},
             {"tool": "indicators", "args": {"tf": "4h"}}],
        ])
        out = r._chat_with_tools("sys", "user")
        self.assertEqual(out, "PLAN")
        self.assertEqual(len(r.prompts), 3, "应该是 3 次调用：原始 + 补数 + 补覆盖")
        self.assertIn("补数据", r.prompts[-1])
        self.assertIn("indicators:15m", r.prompts[-1])

    def test_single_round_complete_no_retry(self):
        r = self._R([[
            {"tool": "skill", "args": {"name": "brooks-bar-by-bar"}},
            {"tool": "indicators", "args": {"tf": "5m"}},
            {"tool": "indicators", "args": {"tf": "15m"}},
            {"tool": "indicators", "args": {"tf": "1h"}},
            {"tool": "indicators", "args": {"tf": "4h"}},
        ]])
        out = r._chat_with_tools("sys", "user")
        self.assertEqual(out, "PLAN")
        self.assertEqual(len(r.prompts), 1, "一次到位就不该重试")
