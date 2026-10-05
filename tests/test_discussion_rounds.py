"""讨论轮数：eth-disc 从 3 轮下调到 2 轮（配置层），并钉死调用预算的算法。

为什么要降：一次完整讨论的 LLM 调用数 = 成员数 ×(1 次独立分析 + N 轮辩论)。
eth-disc 是 3 人格，所以 3 轮 = 3×4 = **12 次调用**、2 轮 = 3×3 = **9 次**，
省 25% token，也快约 1/4 的辩论耗时。

更重要的理由不是省钱：**轮数不是提高质量的旋钮**。调研（REVIEW 见
research/trading-system-design-upgrade/REPORT.md）里辩论 vs self-consistency
的对比显示轮数增加收益很快平台化，而**从众（sycophancy）**会随轮数累积 ——
隐藏各 agent 先前的答案会让从众率从 8% 飙到 89%。提高质量要靠增加**异质**
成员，不是靠多辩几轮。

降轮是行为改变，所以对照组（disc-ctl / disc-exp / disc-trio / disc-test）
**保持 3 轮不变**，留作后续 A/B 的基线。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.persona.config import (  # noqa: E402
    DISCUSSION_DEFAULT_ROUNDS,
    DISCUSSION_MAX_ROUNDS,
    DiscussionConfig,
    PersonaGroup,
    load_persona_groups,
)
from omnialpha.persona.runner import PersonaRunner  # noqa: E402

GROUP_CFG = ROOT / "config" / "persona_groups.yaml"


class _CountingRunner:
    """数调用次数，并返回一个**保持原判**的修订。

    必须返回修订（而不是 None）：真实的 `StrategyLoop.discuss` 只要 LLM 回了
    可解析的 JSON 就返回 dict —— **不管立场有没有变**。而 `_discuss` 的循环
    在「本轮没有任何有效修订」时会立刻收尾（`if not discussions`），
    所以返回 None 会让讨论在第 1 轮就结束，测不出轮数预算。
    """

    def __init__(self, calls):
        self._calls = calls

    def discuss(self, plan, peers, round_num, discussion_text, max_rounds=3):
        self._calls.append((round_num,))
        return {"decision": "hold", "confidence": 0.5, "reasoning": "维持原判"}


class _DeadRunner:
    """LLM 一直失败（返回 None）—— 用来钉住「全员无有效修订就收尾」。"""

    def discuss(self, plan, peers, round_num, discussion_text, max_rounds=3):
        return None


def _plan(action, symbol="ETH_USDT"):
    return {
        "ok": True, "decision": action, "reasoning": "原始理由",
        "chips": [{"symbol": symbol, "action": action, "sl": 2694.0,
                   "tp": 2726.0, "size_usd": 400, "type": "limit", "price": 2702.0}],
    }


def _run(members, rounds):
    group = PersonaGroup(
        name="t", members=list(members), target_account=members[0],
        topology="single_account", fusion="weighted_vote", on_conflict="hold",
        discussion=DiscussionConfig(enabled=True, rounds=rounds,
                                    early_exit_on_agreement=False),
    )
    calls: list = []
    runners = {k: _CountingRunner(calls) for k in members}
    with tempfile.TemporaryDirectory() as td:
        r = PersonaRunner(Path(td), group, {}, runners)
        _, used = r._discuss({k: _plan("hold") for k in members})
        return used, r.discussion_log, len(calls)


class TestShippedConfig(unittest.TestCase):
    def _groups(self):
        return {g.name: g for g in load_persona_groups(GROUP_CFG)}

    def test_ethdisc_is_two_rounds(self):
        g = self._groups()["eth-disc"]
        self.assertTrue(g.discussion.enabled)
        self.assertEqual(g.discussion.rounds, 2, "eth-disc 应为 2 轮")

    def test_ethdisc_still_does_not_early_exit(self):
        """降轮不能顺手把「不早退」关掉 —— 否则审计看不到完整辩论过程。"""
        g = self._groups()["eth-disc"]
        self.assertFalse(g.discussion.early_exit_on_agreement)

    def test_control_groups_keep_three_rounds(self):
        """对照组保持 3 轮，留作 A/B 基线。"""
        groups = self._groups()
        for name in ("disc-trio", "disc-test", "disc-ctl", "disc-exp"):
            self.assertEqual(groups[name].discussion.rounds, 3,
                             f"{name} 是对照组，不该被改动")

    def test_only_ethdisc_was_changed(self):
        """全配置里「讨论开着且轮数 != 3」的只有 eth-disc 一家。"""
        groups = self._groups()
        non_three = sorted(
            n for n, g in groups.items()
            if g.discussion.enabled and g.discussion.rounds != 3
        )
        self.assertEqual(non_three, ["eth-disc"])


class TestRoundBudget(unittest.TestCase):
    def test_two_rounds_two_records(self):
        used, log, _ = _run(["a", "b", "c"], rounds=2)
        self.assertEqual(used, 2)
        self.assertEqual(len(log), 3, "1 条表头 + 2 条轮记录")

    def test_three_rounds_three_records(self):
        used, log, _ = _run(["a", "b", "c"], rounds=3)
        self.assertEqual(used, 3)
        self.assertEqual(len(log), 4)

    def test_debate_calls_are_members_times_rounds(self):
        for rounds in (1, 2, 3, 4):
            _, _, calls = _run(["a", "b", "c"], rounds=rounds)
            self.assertEqual(calls, 3 * rounds,
                             f"{rounds} 轮 × 3 人应产生 {3 * rounds} 次辩论调用")

    def test_three_to_two_saves_a_quarter_of_total_calls(self):
        """总调用 = 成员数 ×(1 分析 + N 辩论)：3 人时 12 → 9 次，省 25%。"""
        members = 3
        _, _, calls3 = _run(["a"] * members, rounds=3)
        _, _, calls2 = _run(["a"] * members, rounds=2)
        total3 = members * 1 + calls3          # 3 次独立分析 + 9 次辩论
        total2 = members * 1 + calls2          # 3 次独立分析 + 6 次辩论
        self.assertEqual((total3, total2), (12, 9))
        self.assertAlmostEqual(1 - total2 / total3, 0.25)

    def test_more_members_cost_more_than_more_rounds(self):
        """加一个人（异质）比加一轮贵得少 —— 这是「轮数不是有效旋钮」的算术侧。"""
        _, _, three_by_three = _run(["a", "b", "c"], rounds=3)   # 9
        _, _, four_by_two = _run(["a", "b", "c", "d"], rounds=2)  # 8
        self.assertLess(four_by_two, three_by_three)


class TestStageLabels(unittest.TestCase):
    """阶段标签必须跟着**实际轮数**走。

    降轮暴露的口径漂移：原先日志里按固定 round_num 映射阶段名
    （1=相互讨论、2=深化、其余=最终决策），而 `StrategyLoop.discuss` 是按
    `min(round_num, max_rounds)` 给提示词的。轮数=3 时两者恰好重合，
    降到 2 之后最后一轮日志标「深化讨论」、模型却被告知「最终决策」。
    """

    def _stages(self, rounds):
        _, log, _ = _run(["a", "b", "c"], rounds=rounds)
        return [rec["stage"] for rec in log[1:]]

    def test_two_rounds_last_is_final(self):
        self.assertEqual(self._stages(2), ["相互讨论与反驳", "最终决策"])

    def test_three_rounds_has_middle(self):
        self.assertEqual(self._stages(3), ["相互讨论与反驳", "深化讨论", "最终决策"])

    def test_four_rounds_middle_repeats(self):
        self.assertEqual(
            self._stages(4),
            ["相互讨论与反驳", "深化讨论", "深化讨论", "最终决策"])

    def test_single_round_is_first(self):
        self.assertEqual(self._stages(1), ["相互讨论与反驳"])

    def test_last_round_always_labelled_final(self):
        """不管配几轮，最后一条轮记录的阶段名都必须是「最终决策」。"""
        for rounds in (1, 2, 3, 4):
            self.assertEqual(self._stages(rounds)[-1],
                             "相互讨论与反驳" if rounds == 1 else "最终决策",
                             f"rounds={rounds} 的末轮阶段名不对")


class TestClamping(unittest.TestCase):
    def test_zero_clamps_to_one(self):
        self.assertEqual(DiscussionConfig(enabled=True, rounds=0).rounds, 1)

    def test_negative_clamps_to_one(self):
        self.assertEqual(DiscussionConfig(enabled=True, rounds=-5).rounds, 1)

    def test_huge_clamps_to_max(self):
        self.assertEqual(DiscussionConfig(enabled=True, rounds=99).rounds,
                         DISCUSSION_MAX_ROUNDS)

    def test_default_is_two(self):
        self.assertEqual(DiscussionConfig(enabled=True).rounds,
                         DISCUSSION_DEFAULT_ROUNDS)

    def test_single_round_runs_once(self):
        used, log, calls = _run(["a", "b"], rounds=1)
        self.assertEqual((used, len(log), calls), (1, 2, 2))

    def test_all_dead_runners_stop_after_one_round(self):
        """全员都没有有效修订 → 第 1 轮就收尾，哪怕配了 4 轮。

        这是既有行为（`_discuss` 的 `if not discussions` 早退），**不受
        `early_exit_on_agreement` 控制** —— 那个开关只管「立场已一致」。
        也就是说：`rounds` 是**上限**，实际轮数还要看每轮有没有人回得来。
        """
        group = PersonaGroup(
            name="t", members=["a", "b"], target_account="a",
            topology="single_account", fusion="weighted_vote", on_conflict="hold",
            discussion=DiscussionConfig(enabled=True, rounds=4,
                                        early_exit_on_agreement=False),
        )
        runners = {k: _DeadRunner() for k in ("a", "b")}
        with tempfile.TemporaryDirectory() as td:
            r = PersonaRunner(Path(td), group, {}, runners)
            _, used = r._discuss({k: _plan("hold") for k in ("a", "b")})
            self.assertEqual(used, 1)
            self.assertEqual(len(r.discussion_log), 2, "1 条表头 + 1 条轮记录")


if __name__ == "__main__":
    unittest.main()
