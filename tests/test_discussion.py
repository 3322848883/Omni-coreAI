"""讨论模式测试：可选参数、轮次上限、提前终止、融合集成。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.persona import (  # noqa: E402
    DiscussionConfig,
    PersonaGroup,
    SharedOrderStore,
    PersonaRunner,
)
from omnialpha.persona.config import (  # noqa: E402
    DISCUSSION_DEFAULT_ROUNDS,
    DISCUSSION_MAX_ROUNDS,
    load_persona_groups,
)
from omnialpha.persona.fusion import DIR_HOLD  # noqa: E402


class FakePlanRunner:
    """可编程的假 PlanRunner（支持 discuss 修正）。"""
    def __init__(self, decision="long", confidence=0.8, symbol="BTC_USDT",
                 size_usd=100, discuss_result=None):
        self.decision = decision
        self.confidence = confidence
        self.symbol = symbol
        self.size_usd = size_usd
        self.discuss_result = discuss_result
        self.discuss_calls = 0

    def analyze_once(self, trigger="manual"):
        return {"ok": True, "cycle_id": f"c-{id(self)}", "plan": {
            "cycle_id": f"c-{id(self)}", "decision": self.decision,
            "confidence": self.confidence, "reasoning": f"初始判断: {self.decision}",
            "chips": [{"action": f"open_{self.decision}", "symbol": self.symbol,
                        "size_usd": self.size_usd, "confidence": self.confidence}],
        }}

    def discuss(self, plan, peers, round_num, discussion_text, max_rounds=3):
        self.discuss_calls += 1
        if self.discuss_result is not None:
            return self.discuss_result
        return None


def _group(**kw):
    base = dict(name="g", members=["a", "b"], topology="single_account", target_account="a", fusion="weighted_vote",
                fusion_config={"weights": {"a": 1, "b": 1}}, on_conflict="hold")
    if "discussion" not in kw:
        kw["discussion"] = DiscussionConfig(enabled=True, rounds=2)
    base.update(kw)
    return PersonaGroup(**base)


class TestDiscussionConfig(unittest.TestCase):
    def test_default_disabled(self):
        g = PersonaGroup(name="x", members=["a", "b"])
        self.assertFalse(g.discussion.enabled)

    def test_default_rounds(self):
        self.assertEqual(DISCUSSION_DEFAULT_ROUNDS, 2)
        self.assertEqual(DISCUSSION_MAX_ROUNDS, 4)

    def test_load_from_yaml(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text(
                "groups:\n"
                "  - name: disc\n    members: [a, b]\n    topology: single_account\n"
                "    target_account: a\n"
                "    discussion:\n      enabled: true\n      rounds: 3\n      timeout_sec: 60\n",
                encoding="utf-8",
            )
            groups = load_persona_groups(p)
            self.assertTrue(groups[0].discussion.enabled)
            self.assertEqual(groups[0].discussion.rounds, 3)
            self.assertEqual(groups[0].discussion.timeout_sec, 60)

    def test_rounds_clamped_to_max(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text(
                "groups:\n"
                "  - name: x\n    members: [a, b]\n    topology: single_account\n"
                "    target_account: a\n"
                "    discussion:\n      enabled: true\n      rounds: 10\n",
                encoding="utf-8",
            )
            groups = load_persona_groups(p)
            self.assertEqual(groups[0].discussion.rounds, DISCUSSION_MAX_ROUNDS)

    def test_yaml_without_discussion(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text("groups:\n  - name: x\n    members: [a, b]\n", encoding="utf-8")
            groups = load_persona_groups(p)
            self.assertFalse(groups[0].discussion.enabled)


class TestDiscussionFlow(unittest.TestCase):
    def test_discussion_disabled_by_default(self):
        """默认不开讨论，行为与纯投票一致。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = PersonaGroup(name="g", members=["a", "b"], topology="single_account",
                            target_account="a", fusion="weighted_vote",
                            fusion_config={"weights": {"a": 1, "b": 1}})
            self.assertFalse(g.discussion.enabled)
            runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("long")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            self.assertNotIn("discussion_rounds", res.get("fusion", {}))

    def test_discussion_enabled_calls_discuss(self):
        """开启讨论后 discuss 方法被调用。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group()
            ra = FakePlanRunner("long")
            rb = FakePlanRunner("short")
            runners = {"a": ra, "b": rb}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            # discuss 被调用
            self.assertGreater(ra.discuss_calls + rb.discuss_calls, 0)

    def test_discussion_revises_decision(self):
        """讨论后决策被修正。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group()
            # a 初始 long，讨论后改为 short
            ra = FakePlanRunner("long", discuss_result={
                "decision": "short", "reasoning": "听了 b 的空头论点，修正为 short",
                "chips": [{"action": "open_short", "symbol": "BTC_USDT",
                            "size_usd": 100, "confidence": 0.8}],
            })
            rb = FakePlanRunner("short")
            runners = {"a": ra, "b": rb}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            # 讨论后两票都是 short → 融合应为 short
            self.assertEqual(res["decision"], "short")

    def test_discussion_rounds_capped(self):
        """讨论轮次绝不超过配置上限。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(discussion=DiscussionConfig(enabled=True, rounds=2))
            ra = FakePlanRunner("long")
            rb = FakePlanRunner("short")
            runners = {"a": ra, "b": rb}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            rounds = res.get("fusion", {}).get("discussion_rounds", 0)
            self.assertLessEqual(rounds, 2)

    def test_discussion_early_exit_on_agreement(self):
        """首轮一致则提前终止（不多跑一轮）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(discussion=DiscussionConfig(enabled=True, rounds=3))
            # 两人都 long，不修正
            ra = FakePlanRunner("long")
            rb = FakePlanRunner("long")
            runners = {"a": ra, "b": rb}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            rounds = res.get("fusion", {}).get("discussion_rounds", 0)
            # 第 1 轮初始分析后就一致 → discuss 只跑 0 轮修正（early exit）
            self.assertLessEqual(rounds, 1)

    def test_discussion_with_no_discuss_support(self):
        """runner 不支持 discuss 时保持原判，不崩溃。"""
        class NoDiscussRunner:
            def analyze_once(self, trigger="manual"):
                return {"ok": True, "cycle_id": "c-1", "plan": {
                    "cycle_id": "c-1", "decision": "long", "confidence": 0.8,
                    "chips": [{"action": "open_long", "symbol": "BTC_USDT",
                                "size_usd": 50, "confidence": 0.8}],
                }}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group()
            runners = {"a": NoDiscussRunner(), "b": NoDiscussRunner()}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])

    def test_discussion_respects_max_4(self):
        """即使配置 rounds=10，也只跑最多 4 轮。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(discussion=DiscussionConfig(enabled=True, rounds=10))
            self.assertEqual(g.discussion.rounds, DISCUSSION_MAX_ROUNDS)
            ra = FakePlanRunner("long")
            rb = FakePlanRunner("short")
            runners = {"a": ra, "b": rb}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            rounds = res.get("fusion", {}).get("discussion_rounds", 0)
            self.assertLessEqual(rounds, 4)


class TestDiscussionIntegration(unittest.TestCase):
    def test_discussion_with_memory(self):
        """讨论模式 + 记忆系统：lifecycle/journal 正常。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group()
            ra = FakePlanRunner("long")
            rb = FakePlanRunner("long")
            runners = {"a": ra, "b": rb}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            oid = res["order_id"]
            self.assertIsNotNone(oid)
            store = SharedOrderStore(root)
            got = store.get(oid)
            self.assertGreaterEqual(len(got.get("lifecycle", [])), 1)


if __name__ == "__main__":
    unittest.main()
