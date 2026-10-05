"""系统提示词的结构契约（归属标注 + 规则完整性）。

背景：提示词是 AI 唯一的约束来源，但**改动它没有任何护栏** —— 删掉一条规则、
漏掉一个归属标签，都不会有测试失败。参照 nofx 的 `engine_prompt_test.go`
（断言必备短语存在、违禁短语不存在），为 `strategist/prompt.py` 加同类护栏。

**动因**：规则 14 让 AI 去撤孤儿保护单，而 AI 因「position_open 说明 protections
属于持仓」而不敢撤 —— 卡了 6 小时。归属标注就是为了不再重演。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.prompt import SYSTEM_PROMPT  # noqa: E402

RULE_COUNT = 17


class TestPromptContract(unittest.TestCase):
    def test_ownership_tags_declared(self):
        """三类归属标签必须在规则区开头声明。"""
        for tag in ("[代码强制]", "[代码兜底]", "[AI 判断]"):
            self.assertIn(tag, SYSTEM_PROMPT, f"缺归属标签声明 {tag}")

    def test_every_rule_has_ownership_tag(self):
        """每条规则都要带归属标签 —— 否则模型无法判断该不该自己做。"""
        for n in range(1, RULE_COUNT + 1):
            m = re.search(rf"(?:^|\n){n}\) ([^\n]*)", SYSTEM_PROMPT)
            self.assertIsNotNone(m, f"缺规则 {n}")
            self.assertTrue(
                m.group(1).startswith("["),
                f"规则 {n} 缺归属标签：{m.group(1)[:40]!r}")

    def test_state_and_action_set_rule_present(self):
        """规则 17（状态与动作集 + 保护单卫生）必须在 —— 它填补了两个空白。"""
        self.assertIn("状态与动作集", SYSTEM_PROMPT)
        self.assertIn("保护单卫生", SYSTEM_PROMPT)

    def test_protection_hygiene_is_delegated_to_engine(self):
        """保护单超额必须**归给引擎**，而不是让 AI 去做机械活。

        这是本规则的关键设计：AI 只需「知道」并**不要误判持仓大小**，
        清理由引擎负责 —— 否则会重演规则 14 那种「AI 想做但不敢做」的僵局。
        """
        self.assertIn("引擎会自动对齐与清理", SYSTEM_PROMPT)
        self.assertIn("不必逐条处理", SYSTEM_PROMPT)

    def test_open_position_allows_management(self):
        """`position_open` 的动作集必须写明（原先是个空白）。"""
        self.assertIn("position_open`=可管理", SYSTEM_PROMPT)
        self.assertIn("会被引擎映射为加仓", SYSTEM_PROMPT)

    def test_core_hard_rules_intact(self):
        """核心硬约束不能被顺手删掉。"""
        for phrase in (
            "action 英文枚举",
            "止损止盈用 tp/sl",
            "同时最多 5 个生效",
            "position_state",
            "必须调用行情工具",
            "type=limit 等必须给 price",
        ):
            self.assertIn(phrase, SYSTEM_PROMPT, f"核心规则缺失：{phrase}")

    def test_unknown_state_still_forbids_cancelling_protection(self):
        """`unknown` 时禁止撤保护单这条安全约束必须保留。"""
        self.assertIn("禁止撤销任何 tp/sl 保护单", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
