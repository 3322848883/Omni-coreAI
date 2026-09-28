"""多人格共管测试：配置/融合/共享订单/执行映射。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.persona import (  # noqa: E402
    PersonaError,
    PersonaGroup,
    SharedOrderStore,
    fuse_plans,
    load_persona_groups,
    validate_group,
)
from gate_bot.persona.config import member_weight  # noqa: E402
from gate_bot.persona.fusion import DIR_HOLD, DIR_LONG, DIR_SHORT  # noqa: E402


class TestConfig(unittest.TestCase):
    def test_load_two_topologies(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text(
                "groups:\n"
                "  - name: single\n    topology: single_account\n"
                "    members: [a, b]\n    target_account: a\n    fusion: weighted_vote\n"
                "  - name: mirror\n    topology: mirror_accounts\n"
                "    members: [a, b, c]\n    fusion: consensus\n"
                "    fusion_config: {threshold: 0.7}\n",
                encoding="utf-8",
            )
            groups = load_persona_groups(p)
            self.assertEqual(len(groups), 2)
            self.assertEqual(groups[0].topology, "single_account")
            self.assertEqual(groups[1].topology, "mirror_accounts")
            self.assertEqual(groups[1].fusion, "consensus")

    def test_members_must_be_at_least_two(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text("groups:\n  - name: x\n    members: [a]\n", encoding="utf-8")
            with self.assertRaises(PersonaError):
                load_persona_groups(p)

    def test_unknown_member_rejected(self):
        g = PersonaGroup(name="g", members=["a", "ghost"])
        with self.assertRaises(PersonaError):
            validate_group(g, known_bots={"a", "b"})


class TestFusion(unittest.TestCase):
    def _group(self, **kw):
        base = dict(name="g", members=["a", "b", "c"], fusion="weighted_vote",
                    fusion_config={"weights": {"a": 1, "b": 1, "c": 1}})
        base.update(kw)
        return PersonaGroup(**base)

    def test_weighted_vote_majority(self):
        plans = {"a": {"decision": "long"}, "b": {"decision": "long"}, "c": {"decision": "short"}}
        r = fuse_plans(self._group(), plans)
        self.assertEqual(r["decision"], DIR_LONG)
        self.assertEqual(r["mode"], "weighted_vote")

    def test_weighted_conflict_defaults_hold(self):
        plans = {"a": {"decision": "long"}, "b": {"decision": "short"}, "c": {"decision": "hold"}}
        r = fuse_plans(self._group(), plans, on_conflict="hold")
        self.assertEqual(r["decision"], DIR_HOLD)

    def test_weighted_conflict_majority(self):
        plans = {"a": {"decision": "long"}, "b": {"decision": "long"}, "c": {"decision": "short"}}
        g = self._group(fusion_config={"weights": {"a": 1, "b": 1, "c": 5}})
        r = fuse_plans(g, plans, on_conflict="majority")
        # c 权重 5 > a+b=2
        self.assertEqual(r["decision"], DIR_SHORT)

    def test_master_arbiter(self):
        g = self._group(fusion="master_arbiter", fusion_config={"master": "a"})
        plans = {"a": {"decision": "short"}, "b": {"decision": "long"}, "c": {"decision": "long"}}
        r = fuse_plans(g, plans)
        self.assertEqual(r["decision"], DIR_SHORT)
        self.assertEqual(r["master"], "a")

    def test_consensus_threshold(self):
        g = self._group(fusion="consensus", fusion_config={"threshold": 0.7})
        plans = {"a": {"decision": "long"}, "b": {"decision": "long"}, "c": {"decision": "short"}}
        r = fuse_plans(g, plans)
        # 2/3 ≈ 0.67 < 0.7 → hold
        self.assertEqual(r["decision"], DIR_HOLD)

    def test_member_weight_default(self):
        g = self._group(fusion_config={})
        self.assertEqual(member_weight(g, "a"), 1.0)


class TestSharedOrders(unittest.TestCase):
    def test_lifecycle_and_votes(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"]})
            oid = rec["order_id"]
            self.assertTrue(oid.startswith("o-"))
            store.record_vote(oid, "c-1", "a", "long", reasoning="突破")
            store.record_vote(oid, "c-1", "b", "hold", reasoning="等待")
            store.append_log(oid, "fusion_decision", "weighted_vote→long")
            got = store.get(oid)
            self.assertEqual(got["votes"]["c-1"]["a"]["decision"], "long")
            self.assertEqual(got["reason"]["b"], "等待")
            self.assertEqual(len(got["log"]), 1)
            self.assertEqual(len(store.list_open()), 1)
            store.update(oid, status="closed")
            self.assertEqual(len(store.list_open()), 0)


class TestRunnerExec(unittest.TestCase):
    def test_single_account_dedup(self):
        """single_account 只写 target 的 inbox（去重）。"""
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = PersonaGroup(
                name="g", members=["a", "b"], topology="single_account",
                target_account="a", fusion="weighted_vote",
                fusion_config={"weights": {"a": 1, "b": 1}},
            )

            class FakeRunner:
                def __init__(self, decision):
                    self.decision = decision

                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-1", "plan": {
                        "cycle_id": "c-1", "decision": self.decision,
                        "confidence": 0.8,
                        "chips": [{"action": "open_long", "symbol": "BTC_USDT",
                                   "size_usd": 100, "confidence": 0.8}],
                    }}

            runners = {"a": FakeRunner("long"), "b": FakeRunner("long")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["executed"])
            self.assertEqual(res["topology"], "single_account")
            inbox_a = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            inbox_b = list((root / "data" / "bots" / "b" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_a), 1)  # 只落 target
            self.assertEqual(len(inbox_b), 0)

    def test_mirror_broadcast(self):
        """mirror_accounts 广播到各成员。"""
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = PersonaGroup(
                name="g", members=["a", "b"], topology="mirror_accounts",
                fusion="weighted_vote", fusion_config={"weights": {"a": 1, "b": 1}},
            )

            class FakeRunner:
                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-1", "plan": {
                        "cycle_id": "c-1", "decision": "long", "confidence": 0.9,
                        "chips": [{"action": "open_long", "symbol": "BTC_USDT", "size_usd": 50}],
                    }}

            runners = {"a": FakeRunner(), "b": FakeRunner()}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["executed"])
            self.assertEqual(res["topology"], "mirror_accounts")
            self.assertEqual(len(res["delivered"]), 2)
            for b in ("a", "b"):
                self.assertTrue(list((root / "data" / "bots" / b / "inbox").glob("*.json")), b)

    def test_conflict_hold_no_execute(self):
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = PersonaGroup(
                name="g", members=["a", "b"], topology="single_account",
                target_account="a", fusion="weighted_vote",
                fusion_config={"weights": {"a": 1, "b": 1}}, on_conflict="hold",
            )

            class R:
                def __init__(self, d):
                    self.d = d

                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-1", "plan": {
                        "cycle_id": "c-1", "decision": self.d, "confidence": 0.7,
                        "chips": [{"action": f"open_{self.d}", "symbol": "BTC_USDT"}],
                    }}

            runners = {"a": R("long"), "b": R("short")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertEqual(res["decision"], DIR_HOLD)
            self.assertFalse(res.get("executed"))


if __name__ == "__main__":
    unittest.main()
