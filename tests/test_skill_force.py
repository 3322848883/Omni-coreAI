"""skills_required 首轮 tool_choice 硬制（2026-10-12 brooks-eth 0/5 后续）。

背景：hint 升级「必载」文案实盘 0/5 生效（native tool_choice=auto 模型自选）。
本组测试锁定：required=true 时首轮 tool_choice 钉死 skill 函数、后续轮恢复
auto、网关拒绝 named choice 时回退 auto；默认关闭行为逐字不变。

**2026-10-14（N2）**：本文件此前是 pytest 风格（类未继承 `TestCase`），
`unittest` 收集到 **0 个测试** → 在只跑 unittest 的流程里**静默不执行**。
已改写为 unittest 兼容（`TestCase` + `self.assert*`），两种 runner 下都能跑。
"""
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist import loop as loop_mod  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402

REPO = ROOT

SKILL_TOOL = {"type": "function", "function": {"name": "skill"}}
KLINE_TOOL = {"type": "function", "function": {"name": "klines"}}
FORCED = {"type": "function", "function": {"name": "skill"}}


class FakeLLM:
    """按剧本回放；记录每次 tool_choice 供断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat_message_full(self, messages, tools=None, tool_choice=None):
        self.calls.append({"tool_choice": tool_choice,
                           "tools": [t["function"]["name"] for t in (tools or [])]})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _runner(**kw):
    cfg = StrategistConfig(bot_id="t", bot_root=REPO, **kw)
    r = PlanRunner.__new__(PlanRunner)
    r.cfg = cfg
    return r


class TestForcedSkillChoice(unittest.TestCase):
    def test_default_off_returns_none(self):
        tools = [SKILL_TOOL, KLINE_TOOL]
        self.assertIsNone(PlanRunner._forced_skill_choice(tools))

    def test_required_without_skills_returns_none(self):
        self.assertIsNone(
            PlanRunner._forced_skill_choice([SKILL_TOOL], skills_required=True))
        self.assertIsNone(
            PlanRunner._forced_skill_choice([SKILL_TOOL], skills_required=True,
                                            skill_ids=[]))

    def test_skill_tool_absent_returns_none(self):
        """skill 不在工具面（被 allow 过滤）时绝不能硬制——否则请求必炸。"""
        self.assertIsNone(PlanRunner._forced_skill_choice(
            [KLINE_TOOL], skills_required=True,
            skill_ids=["price-action-trading"]))

    def test_forced_dict_shape(self):
        out = PlanRunner._forced_skill_choice(
            [SKILL_TOOL, KLINE_TOOL], skills_required=True,
            skill_ids=["price-action-trading"])
        self.assertEqual(out, FORCED)


class TestChatNativeToolsForce(unittest.TestCase):
    def _base_tools(self):
        return [SKILL_TOOL, KLINE_TOOL]

    def _run(self, runner, script):
        llm = FakeLLM(script)
        runner.llm = llm
        with mock.patch.object(loop_mod, "available_native_tools",
                               return_value=self._base_tools()):
            out = runner._chat_native_tools(
                [{"role": "user", "content": "hi"}], max_rounds=3)
        return llm, out

    def test_required_first_round_forced_then_auto(self):
        """首轮硬制 skill；模型交回工具调用后，次轮恢复 auto。"""
        r = _runner(skills=["price-action-trading"], skills_required=True)
        r.client = None  # run_tool 已被 mock，但实参求值仍会取该属性
        r._record_tool_use = lambda *a, **k: None
        script = [
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "skill", "arguments": '{"name": "price-action-trading"}'}}]},
            {"content": "{}", "tool_calls": []},
        ]
        with mock.patch.object(loop_mod, "run_tool", return_value={"content": "x"}):
            llm, out = self._run(r, script)
        self.assertEqual(out, "{}")
        self.assertEqual(llm.calls[0]["tool_choice"], FORCED)
        self.assertEqual(llm.calls[1]["tool_choice"], "auto")

    def test_default_off_all_auto(self):
        """默认关闭：逐字旧行为，全轮 auto。"""
        r = _runner(skills=["price-action-trading"])
        r._record_tool_use = lambda *a, **k: None
        llm, out = self._run(r, [{"content": "{}", "tool_calls": []}])
        self.assertEqual(out, "{}")
        self.assertEqual(llm.calls[0]["tool_choice"], "auto")

    def test_provider_rejects_forced_falls_back_auto(self):
        """网关不认 named tool_choice → 回退 auto，本轮不炸。"""
        r = _runner(skills=["price-action-trading"], skills_required=True)
        r._record_tool_use = lambda *a, **k: None
        llm, out = self._run(r, [
            RuntimeError("tool_choice unsupported"),
            {"content": "{}", "tool_calls": []},
        ])
        self.assertEqual(out, "{}")
        self.assertEqual([c["tool_choice"] for c in llm.calls], [FORCED, "auto"])

    def test_no_tool_call_round_still_forced(self):
        """首轮即使直接出 Plan（无工具调用），请求也是硬制发出的。"""
        r = _runner(skills=["price-action-trading"], skills_required=True)
        llm, out = self._run(r, [{"content": "{}", "tool_calls": []}])
        self.assertEqual(out, "{}")
        self.assertEqual(llm.calls[0]["tool_choice"], FORCED)


if __name__ == "__main__":
    unittest.main()
