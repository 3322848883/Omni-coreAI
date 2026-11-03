# -*- coding: utf-8 -*-
"""提示词分层护栏（2026-11-04）。

用户口径：
> 执行契约在系统（通用下单指令怎么下，确保正确执行每一份方案，交易所一共就
> 那么多个指令）；策略模板在人格（下什么单、下多少、什么方式下）；工具层是
> 指标与技能。

三层各管一件事，**不许互相渗透**：

| 层 | 载体 | 负责 | 不该有 |
|---|---|---|---|
| 系统层 | `strategist/prompt.py::_SYSTEM_HEAD` | 输出格式、订单原语、字段硬约束、归属标签 | 任何具体策略参数（点位/结构/择时） |
| 人格层 | `prompts/<persona>.md`（`【策略人格】` 之后） | 下什么单、多少、什么价、什么条件 | 引擎下单细节、触发单参数拼装 |
| 工具层 | `tools_guide` + `skill_catalog`（系统层末尾） | 指标/技能目录与用法 | 决策规则本身 |

本测试锁：① 执行契约必须在系统层且覆盖全部订单原语；② 契约里点名的动作
必须都在 `CHIP_ACTIONS` 里（否则「系统收不了」）；③ 人格只能出现在末尾，
不能混进系统头部。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.prompt import (  # noqa: E402
    SYSTEM_PROMPT, build_system_prompt)
from omnialpha.strategist.schema import CHIP_ACTIONS  # noqa: E402


class TestExecutionContractInSystemLayer(unittest.TestCase):
    def test_contract_block_present(self):
        self.assertIn("执行契约", SYSTEM_PROMPT,
                      "执行契约必须在系统层（所有策略共用）")

    def test_six_primitives_enumerated(self):
        """交易所就这几类指令 —— 少一类，对应意图的计划就会被拒。"""
        for kw in ("市价单 market", "限价单 limit", "突破进场",
                   "保护单（条件触发）", "reduce_only", "撤单 / 平仓 / 减仓"):
            self.assertIn(kw, SYSTEM_PROMPT, f"执行契约缺原语：{kw}")

    def test_hard_constraints_documented(self):
        for kw in ("size_step", "order_price_round", "trigger_price",
                   "max_notional_usd"):
            self.assertIn(kw, SYSTEM_PROMPT, f"执行契约缺字段约束：{kw}")

    def test_named_actions_all_executable(self):
        """契约里点名的动作必须都在 `CHIP_ACTIONS` 里（系统真的收得下）。"""
        named = ("stop_entry_long", "stop_entry_short", "modify_tp_sl",
                 "cancel_all", "cancel_price_all", "close_all", "flatten",
                 "reduce_long", "reduce_short")
        for a in named:
            self.assertIn(a, CHIP_ACTIONS, f"契约写了 {a}，schema 却不接受")


class TestLayerSeparation(unittest.TestCase):
    def test_persona_after_tools(self):
        """系统层 → 工具层 → 技能目录 → 【策略人格】：顺序即分层。"""
        out = build_system_prompt("PERSONA_TEXT", tools_guide="TOOLS_TEXT",
                                  skill_catalog="SKILLS_TEXT")
        i_sys = out.index("执行契约")
        i_tools = out.index("TOOLS_TEXT")
        i_skill = out.index("SKILLS_TEXT")
        i_persona = out.index("【策略人格】")
        self.assertLess(i_sys, i_tools)
        self.assertLess(i_tools, i_skill)
        self.assertLess(i_skill, i_persona)
        self.assertIn("PERSONA_TEXT", out)

    def test_persona_not_in_system_head(self):
        """人格内容不能出现在固定系统头里（否则换人格会改到系统层）。"""
        self.assertNotIn("【策略人格】", SYSTEM_PROMPT)

    def test_strategy_agnostic(self):
        """系统层不许出现具体策略参数 —— 那是人格的活。"""
        for kw in ("马丁", "底仓", "网格层", "SMC 结构位"):
            self.assertNotIn(kw, SYSTEM_PROMPT, f"系统层混入了策略参数：{kw}")


if __name__ == "__main__":
    unittest.main()
