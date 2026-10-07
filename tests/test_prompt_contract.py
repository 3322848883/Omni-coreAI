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

RULE_COUNT = 19


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


class TestTriggerParamRangesAreConsistent(unittest.TestCase):
    """契约里同一个参数的范围只能有一个说法。

    原先一处写 `price_break{lookback:20-300}`、另一处写「lookback 必须 5-300」——
    模型看到两个范围，可能取 5–20 之间的值，然后被 `trigger_store` 的校验整条拒掉
    （那个下限在 2026-10-06 已从 5 提到 20，当时只改了前一处）。
    """

    def test_lookback_lower_bound_documented_as_20(self):
        self.assertIn("lookback 必须 20-300", SYSTEM_PROMPT)

    def test_no_stale_lower_bound(self):
        self.assertNotIn("5-300", SYSTEM_PROMPT,
                         "契约里还留着旧的 lookback 下限（已提到 20）")


class TestOnlyManageOwnSymbols(unittest.TestCase):
    """契约必须明确「只管理自己品种宇宙内的 symbol」。

    实测（2026-10-07）：把品种换成 SOL 后，账户里遗留的 BTC 挂单被模型判为
    「不属于当前宇宙的遗留单」，连续两轮发出 `cancel_all`。而账户可能是共享的
    （多 bot 共账户）—— 宇宙外的单**本来就不是这个 bot 的**，撤它们会破坏别人。
    """

    def test_rule_exists(self):
        self.assertIn("只管理自己的品种", SYSTEM_PROMPT)

    def test_universe_is_named_as_the_boundary(self):
        self.assertIn("【品种宇宙】", SYSTEM_PROMPT)

    def test_forbids_every_mutating_action_on_others(self):
        for act in ("cancel_", "close_", "reduce_", "modify_tp_sl", "flatten"):
            with self.subTest(act=act):
                self.assertIn(act, SYSTEM_PROMPT)

    def test_says_ignore_rather_than_cleanup(self):
        """不能只是「禁止」，还要给出替代行为 —— 否则模型可能改成别的清理动作。"""
        self.assertIn("只忽略", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
