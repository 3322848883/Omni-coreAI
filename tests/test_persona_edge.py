"""multi-persona 边界 / 故障 / 生命周期全面测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.persona import (  # noqa: E402
    PersonaError,
    PersonaGroup,
    SharedOrderStore,
    fuse_plans,
    load_persona_groups,
    validate_group,
)
from omnialpha.persona.config import member_weight  # noqa: E402
from omnialpha.persona.fusion import (  # noqa: E402
    DIR_CLOSE,
    DIR_HOLD,
    DIR_LONG,
    DIR_MODIFY,
    DIR_REDUCE,
    DIR_SHORT,
    _norm_confidence,
    _norm_dir,
)
from omnialpha.persona.orders import new_order_id  # noqa: E402
from omnialpha.persona.runner import PersonaRunner  # noqa: E402


def _group(**kw):
    base = dict(name="g", members=["a", "b"], fusion="weighted_vote",
                fusion_config={"weights": {"a": 1, "b": 1}}, on_conflict="hold")
    base.update(kw)
    return PersonaGroup(**base)


class FakePlanRunner:
    """可编程的假 PlanRunner。"""
    def __init__(self, decision="hold", action=None, confidence=0.8,
                 symbol="BTC_USDT", size_usd=100, fail=False, no_chips=False):
        self.decision = decision
        if action is not None:
            self.action = action
        elif decision in ("long", "short"):
            self.action = f"open_{decision}"
        else:
            self.action = decision
        self.confidence = confidence
        self.symbol = symbol
        self.size_usd = size_usd
        self.fail = fail
        self.no_chips = no_chips

    def analyze_once(self, trigger="manual"):
        if self.fail:
            raise RuntimeError("LLM timeout")
        chips = [] if self.no_chips else [{
            "action": self.action, "symbol": self.symbol,
            "size_usd": self.size_usd, "confidence": self.confidence,
        }]
        return {"ok": True, "cycle_id": "c-1", "plan": {
            "cycle_id": "c-1", "decision": self.decision,
            "confidence": self.confidence, "chips": chips,
        }}


# ─────────────────────────────────────────────────────
# 1. fusion 归一化边界
# ─────────────────────────────────────────────────────
class TestNormDir(unittest.TestCase):
    def test_empty_plan_is_hold(self):
        self.assertEqual(_norm_dir({}), DIR_HOLD)

    def test_missing_chips_falls_back_decision(self):
        self.assertEqual(_norm_dir({"decision": "long"}), DIR_LONG)
        self.assertEqual(_norm_dir({"decision": "close"}), DIR_CLOSE)

    def test_add_and_stop_entry_map_to_long_short(self):
        self.assertEqual(_norm_dir({"chips": [{"action": "add_long"}]}), DIR_LONG)
        self.assertEqual(_norm_dir({"chips": [{"action": "stop_entry_long"}]}), DIR_LONG)
        self.assertEqual(_norm_dir({"chips": [{"action": "add_short"}]}), DIR_SHORT)
        self.assertEqual(_norm_dir({"chips": [{"action": "stop_entry_short"}]}), DIR_SHORT)

    def test_close_variants_map_to_close(self):
        for a in ("close", "close_all", "flatten", "close_long", "close_short"):
            self.assertEqual(_norm_dir({"chips": [{"action": a}]}), DIR_CLOSE, a)

    def test_reduce_variants(self):
        self.assertEqual(_norm_dir({"chips": [{"action": "reduce_long"}]}), DIR_REDUCE)
        self.assertEqual(_norm_dir({"chips": [{"action": "reduce_short"}]}), DIR_REDUCE)

    def test_unknown_action_is_hold(self):
        self.assertEqual(_norm_dir({"chips": [{"action": "dance"}]}), DIR_HOLD)

    def test_chips_not_list_is_hold(self):
        self.assertEqual(_norm_dir({"decision": "x", "chips": "oops"}), DIR_HOLD)


class TestNormConfidence(unittest.TestCase):
    def test_bounds_clamped(self):
        self.assertEqual(_norm_confidence({"confidence": -5}), 0.0)
        self.assertEqual(_norm_confidence({"confidence": 99}), 1.0)
        self.assertEqual(_norm_confidence({"confidence": "abc"}), 0.0)
        self.assertEqual(_norm_confidence({"confidence": None}), 0.0)

    def test_chip_fallback(self):
        p = {"chips": [{"confidence": 0.6}]}
        self.assertAlmostEqual(_norm_confidence(p), 0.6)


# ─────────────────────────────────────────────────────
# 2. fusion 决策边界
# ─────────────────────────────────────────────────────
class TestFusionEdge(unittest.TestCase):
    def test_empty_plans_all_hold(self):
        r = fuse_plans(_group(), {})
        self.assertEqual(r["decision"], DIR_HOLD)

    def test_on_conflict_master_uses_master_vote(self):
        g = _group(fusion_config={"weights": {"a": 1, "b": 1}, "master": "b"},
                   on_conflict="master")
        plans = {"a": {"decision": "long"}, "b": {"decision": "short"}}
        r = fuse_plans(g, plans, on_conflict="master")
        self.assertEqual(r["decision"], DIR_SHORT)

    def test_close_majority_wins(self):
        plans = {"a": {"decision": "close", "chips": [{"action": "close"}]},
                 "b": {"decision": "close", "chips": [{"action": "close"}]}}
        r = fuse_plans(_group(), plans)
        self.assertEqual(r["decision"], DIR_CLOSE)
        self.assertIn(r["action"], ("close", "close_all", "flatten", "close_long", "close_short"))

    def test_mixed_close_vs_long_conflict_hold(self):
        """close 与 long 冲突 → 默认 hold（不乱动仓位）。"""
        plans = {"a": {"decision": "close", "chips": [{"action": "close"}]},
                 "b": {"decision": "long", "chips": [{"action": "open_long"}]}}
        r = fuse_plans(_group(), plans)
        # 两票各 1 权重，无过半 → hold
        self.assertEqual(r["decision"], DIR_HOLD)

    def test_weighted_close_majority_over_long(self):
        g = _group(fusion_config={"weights": {"a": 3, "b": 1}})
        plans = {"a": {"decision": "close", "chips": [{"action": "close"}]},
                 "b": {"decision": "long", "chips": [{"action": "open_long"}]}}
        r = fuse_plans(g, plans)
        self.assertEqual(r["decision"], DIR_CLOSE)

    def test_consensus_with_manage_actions(self):
        g = _group(fusion="consensus", fusion_config={"threshold": 0.6})
        plans = {"a": {"decision": "close", "chips": [{"action": "close"}], "confidence": 0.9},
                 "b": {"decision": "close", "chips": [{"action": "close"}], "confidence": 0.9}}
        r = fuse_plans(g, plans)
        self.assertEqual(r["decision"], DIR_CLOSE)

    def test_consensus_below_threshold_hold(self):
        """有分歧时共识分 < 阈值 → hold（全票一致 score=1.0 天然过阈值）。"""
        g = _group(members=["a", "b", "c"],
                   fusion="consensus", fusion_config={"threshold": 0.8,
                                                      "weights": {"a": 1, "b": 1, "c": 1}})
        plans = {"a": {"decision": "long"}, "b": {"decision": "long"},
                 "c": {"decision": "short"}}
        r = fuse_plans(g, plans)
        # 2/3 = 0.67 < 0.8 → hold
        self.assertEqual(r["decision"], DIR_HOLD)

    def test_master_arbiter_master_not_in_votes_defaults_hold(self):
        g = _group(fusion="master_arbiter", fusion_config={"master": "ghost"})
        plans = {"a": {"decision": "long"}, "b": {"decision": "long"}}
        r = fuse_plans(g, plans)
        self.assertEqual(r["decision"], DIR_HOLD)

    def test_confidence_max_used_in_weighted(self):
        plans = {"a": {"decision": "long", "confidence": 0.3},
                 "b": {"decision": "long", "confidence": 0.95}}
        r = fuse_plans(_group(), plans)
        self.assertAlmostEqual(r["confidence"], 0.95)

    def test_three_way_split_is_hold(self):
        plans = {"a": {"decision": "long"}, "b": {"decision": "short"},
                 "c": {"decision": "hold"}}
        g = _group(members=["a", "b", "c"],
                   fusion_config={"weights": {"a": 1, "b": 1, "c": 1}})
        r = fuse_plans(g, plans)
        self.assertEqual(r["decision"], DIR_HOLD)


# ─────────────────────────────────────────────────────
# 3. config 校验边界
# ─────────────────────────────────────────────────────
class TestConfigEdge(unittest.TestCase):
    def test_invalid_topology(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text("groups:\n  - name: x\n    members: [a, b]\n    topology: star\n",
                         encoding="utf-8")
            with self.assertRaises(PersonaError):
                load_persona_groups(p)

    def test_invalid_fusion(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text("groups:\n  - name: x\n    members: [a, b]\n    fusion: magic\n",
                         encoding="utf-8")
            with self.assertRaises(PersonaError):
                load_persona_groups(p)

    def test_invalid_on_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text("groups:\n  - name: x\n    members: [a, b]\n    on_conflict: flip\n",
                         encoding="utf-8")
            with self.assertRaises(PersonaError):
                load_persona_groups(p)

    def test_master_not_in_members_rejected(self):
        g = _group(fusion="master_arbiter", fusion_config={"master": "ghost"})
        with self.assertRaises(PersonaError):
            validate_group(g, known_bots={"a", "b"})

    def test_consensus_threshold_out_of_range(self):
        g = _group(fusion="consensus", fusion_config={"threshold": 0})
        with self.assertRaises(PersonaError):
            validate_group(g, known_bots={"a", "b"})
        g2 = _group(fusion="consensus", fusion_config={"threshold": 1.5})
        with self.assertRaises(PersonaError):
            validate_group(g2, known_bots={"a", "b"})

    def test_target_account_not_in_members(self):
        g = _group(topology="single_account", target_account="ghost")
        with self.assertRaises(PersonaError):
            validate_group(g, known_bots={"a", "b"})

    def test_disabled_group_skips_validation(self):
        g = _group(members=["a", "ghost"], enabled=False)
        validate_group(g, known_bots={"a"})  # 不抛

    def test_bot_id_traversal_rejected(self):
        for bad in ("../evil", "a/b", "a\\b", "..", "."):
            g = _group(members=["a", bad])
            with self.assertRaises(PersonaError, msg=bad):
                validate_group(g, known_bots={"a", bad})

    def test_member_weight_bad_value_defaults_1(self):
        g = _group(fusion_config={"weights": {"a": "oops", "b": 2}})
        self.assertEqual(member_weight(g, "a"), 1.0)
        self.assertEqual(member_weight(g, "b"), 2.0)

    def test_single_account_default_target_first_member(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.yaml"
            p.write_text("groups:\n  - name: x\n    members: [a, b]\n    topology: single_account\n",
                         encoding="utf-8")
            groups = load_persona_groups(p)
            self.assertEqual(groups[0].target_account, "a")


# ─────────────────────────────────────────────────────
# 4. SharedOrderStore 边界
# ─────────────────────────────────────────────────────
class TestOrdersEdge(unittest.TestCase):
    def test_new_order_id_format(self):
        oid = new_order_id()
        self.assertTrue(oid.startswith("o-"))
        self.assertEqual(len(oid), 14)

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            for bad in ("../x", "a/b", "a\\b", "..", ""):
                with self.assertRaises(ValueError, msg=bad):
                    store._path(bad)

    def test_update_missing_raises(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            with self.assertRaises(KeyError):
                store.update("o-none", status="closed")

    def test_record_vote_same_cycle_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            store.record_vote(oid, "c-1", "a", "long", reasoning="first")
            store.record_vote(oid, "c-1", "a", "short", reasoning="changed")
            got = store.get(oid)
            self.assertEqual(got["votes"]["c-1"]["a"]["decision"], "short")
            self.assertEqual(got["reason"]["a"], "changed")

    def test_list_open_skips_corrupt_json(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            store.create({"symbol": "BTC_USDT"})
            # 写一个坏文件
            (store.dir / "o-bad.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(len(store.list_open()), 1)

    def test_multiple_open_orders(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            store.create({"symbol": "BTC_USDT"})
            store.create({"symbol": "ETH_USDT"})
            self.assertEqual(len(store.list_open()), 2)

    def test_append_log_accumulates(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedOrderStore(Path(td))
            rec = store.create({"symbol": "BTC_USDT"})
            oid = rec["order_id"]
            for i in range(5):
                store.append_log(oid, "cycle", f"#{i}")
            self.assertEqual(len(store.get(oid)["log"]), 5)


# ─────────────────────────────────────────────────────
# 5. Runner 订单生命周期 / 故障
# ─────────────────────────────────────────────────────
class TestRunnerLifecycle(unittest.TestCase):
    def test_open_then_hold_reuses_order(self):
        """开仓 → hold：hold 轮复用同一 order_id（共同记忆不断）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            # 第 1 轮：开仓
            runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("long")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res1 = r.run_once()
            self.assertTrue(res1["executed"])
            oid1 = res1["order_id"]
            self.assertIsNotNone(oid1)

            # 第 2 轮：hold
            runners2 = {"a": FakePlanRunner("hold", action="hold"),
                        "b": FakePlanRunner("hold", action="hold")}
            r2 = PersonaRunner(root, g, {"a": None, "b": None}, runners2)
            res2 = r2.run_once()
            self.assertFalse(res2.get("executed"))
            self.assertEqual(res2["order_id"], oid1)

    def test_close_then_new_open_mints_new_id(self):
        """平仓后重新开仓 → 新 order_id。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            old = store.create({"symbol": "BTC_USDT", "side": "long", "status": "closed"})
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("short"), "b": FakePlanRunner("short")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["executed"])
            self.assertNotEqual(res["order_id"], old["order_id"])

    def test_hold_with_no_open_order_no_id(self):
        """无持仓 + hold → 无 order_id，不建空单。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("hold", action="hold"),
                        "b": FakePlanRunner("hold", action="hold")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertIsNone(res["order_id"])
            self.assertFalse(res.get("executed"))

    def test_partial_analyze_failure_still_fuses(self):
        """一个 personality 挂了，另一个成功 → 仍能融合执行。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner(fail=True)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            self.assertTrue(res["executed"])
            self.assertEqual(res["decision"], DIR_LONG)

    def test_all_analyze_failed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner(fail=True), "b": FakePlanRunner(fail=True)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "all_analyze_failed")

    def test_missing_runner_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("long")}  # b 没有 runner
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            self.assertFalse(res["plans"]["b"]["ok"])

    def test_no_chips_symbol_defaults_btc(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("long", no_chips=True),
                        "b": FakePlanRunner("long", no_chips=True)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["ok"])
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertTrue(inbox)
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["symbol"], "BTC_USDT")

    def test_mirror_partial_failure_reports(self):
        """镜像拓扑：一个成员写失败 → executed=False 但已成功的仍落盘。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(topology="mirror_accounts", members=["a", "b"],
                       fusion_config={"weights": {"a": 1, "b": 1}})
            runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("long")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            # 让 b 的 inbox 路径变成文件（写入会失败）
            bad = root / "data" / "bots" / "b" / "inbox"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("block", encoding="utf-8")
            res = r.run_once()
            self.assertFalse(res["executed"])
            self.assertEqual(len(res["failed"]), 1)
            self.assertEqual(res["failed"][0]["bot"], "b")
            self.assertEqual(len(res["delivered"]), 1)

    def test_order_status_after_actions(self):
        self.assertEqual(PersonaRunner._order_status_after("close"), "closed")
        self.assertEqual(PersonaRunner._order_status_after("close_all"), "closed")
        self.assertEqual(PersonaRunner._order_status_after("flatten"), "closed")
        self.assertEqual(PersonaRunner._order_status_after("close_long"), "closed")
        self.assertEqual(PersonaRunner._order_status_after("close_short"), "closed")
        self.assertEqual(PersonaRunner._order_status_after("open_long"), "open")
        self.assertEqual(PersonaRunner._order_status_after("reduce_long"), "open")
        self.assertEqual(PersonaRunner._order_status_after("modify_tp_sl"), "open")
        self.assertEqual(PersonaRunner._order_status_after("hold"), "open")

    def test_mirror_close_marks_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"], "group": "g"})
            g = _group(topology="mirror_accounts", members=["a", "b"],
                       fusion_config={"weights": {"a": 1, "b": 1}})
            runners = {"a": FakePlanRunner("close", action="close"),
                        "b": FakePlanRunner("close", action="close")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            self.assertTrue(res["executed"])
            self.assertEqual(store.get(rec["order_id"])["status"], "closed")

    def test_votes_recorded_in_shared_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("short")}
            # 冲突 hold，但票仍应记入共同记忆
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            oid = res.get("order_id")
            if oid:
                store = SharedOrderStore(root)
                got = store.get(oid)
                self.assertIsNotNone(got)
                # 至少有一个 cycle 的投票
                self.assertTrue(got.get("votes"))


# ─────────────────────────────────────────────────────
# 6. 风控边界（补充）
# ─────────────────────────────────────────────────────
class TestRiskEdge(unittest.TestCase):
    def _runner(self, root, risk, action="open_long", decision="long",
                confidence=0.9, size_usd=500):
        class BotCfg:
            strategist = {"risk": risk} if risk else {}
        g = _group(target_account="a", topology="single_account")
        bots = {"a": BotCfg(), "b": BotCfg()}
        runners = {"a": FakePlanRunner(decision, action=action, confidence=confidence,
                                        size_usd=size_usd),
                    "b": FakePlanRunner(decision, action=action, confidence=confidence,
                                        size_usd=size_usd)}
        return PersonaRunner(root, g, bots, runners)

    def test_no_risk_config_passes_through(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = self._runner(root, risk=None, confidence=0.1, size_usd=9999)
            res = r.run_once()
            self.assertTrue(res["executed"])
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["action"], "open_long")
            self.assertEqual(float(payload["size_usd"]), 9999)

    def test_max_notional_none_size_passes(self):
        """size_usd=None 时 max_notional 不拦（无法比较）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = self._runner(root, risk={"max_notional_usd": 100},
                             confidence=0.9, size_usd=None)
            res = r.run_once()
            self.assertTrue(res["executed"])
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            self.assertIsNone(payload.get("size_usd"))
            self.assertNotIn("risk_capped", payload.get("meta", {}))

    def test_reduce_not_gated_by_min_confidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            store.create({"symbol": "BTC_USDT", "side": "long"})
            r = self._runner(root, risk={"min_confidence": 0.99},
                             action="reduce_long", decision="reduce",
                             confidence=0.1)
            res = r.run_once()
            self.assertTrue(res["executed"])
            self.assertEqual(res["action"], "reduce_long")

    def test_modify_not_gated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            store.create({"symbol": "BTC_USDT", "side": "long"})
            r = self._runner(root, risk={"min_confidence": 0.99},
                             action="modify_tp_sl", decision="modify",
                             confidence=0.0)
            res = r.run_once()
            self.assertTrue(res["executed"])
            self.assertEqual(res["action"], "modify_tp_sl")

    def test_risk_reject_still_writes_hold_signal(self):
        """被风控拦下时仍写 hold 信号（可审计），不静默丢弃。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r = self._runner(root, risk={"min_confidence": 0.95},
                             confidence=0.5)
            r.run_once()
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertTrue(inbox)
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["action"], "hold")
            self.assertIn("risk_reject", payload["meta"])


# ─────────────────────────────────────────────────────
# 7. 全生命周期集成：开 → 管理 → 平 → 再开
# ─────────────────────────────────────────────────────
class TestFullLifecycle(unittest.TestCase):
    def test_open_manage_close_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            g = _group(target_account="a", topology="single_account")

            def run(decision, action):
                runners = {"a": FakePlanRunner(decision, action=action),
                            "b": FakePlanRunner(decision, action=action)}
                r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
                return r.run_once()

            # 1) 开多
            res1 = run("long", "open_long")
            self.assertTrue(res1["executed"])
            oid1 = res1["order_id"]
            self.assertEqual(store.get(oid1)["status"], "open")

            # 2) hold（管理）
            res2 = run("hold", "hold")
            self.assertFalse(res2.get("executed"))
            self.assertEqual(res2["order_id"], oid1)

            # 3) 减仓
            res3 = run("reduce", "reduce_long")
            self.assertTrue(res3["executed"])
            self.assertEqual(res3["order_id"], oid1)
            self.assertEqual(store.get(oid1)["status"], "open")

            # 4) 平仓
            res4 = run("close", "close")
            self.assertTrue(res4["executed"])
            self.assertEqual(res4["order_id"], oid1)
            self.assertEqual(store.get(oid1)["status"], "closed")

            # 5) 重新开空 → 新单
            res5 = run("short", "open_short")
            self.assertTrue(res5["executed"])
            oid2 = res5["order_id"]
            self.assertNotEqual(oid2, oid1)
            self.assertEqual(store.get(oid2)["status"], "open")

            # 共同记忆：oid1 有多轮投票与日志
            rec = store.get(oid1)
            self.assertGreaterEqual(len(rec["log"]), 1)
            self.assertTrue(rec["votes"])

            # inbox 信号数量 = 执行次数（1 开 + 1 减 + 1 平 + 1 再开 = 4）
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox), 4)

    def test_persona_log_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("long"), "b": FakePlanRunner("long")}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            r.run_once()
            log_path = root / "data" / "shared" / "persona_log.jsonl"
            self.assertTrue(log_path.exists())
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertIn("event", rec)
            self.assertEqual(rec["group"], "g")

    def test_signal_payload_shape(self):
        """信号 payload 字段完整（执行器可消费）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner("short", symbol="ETH_USDT", size_usd=250),
                        "b": FakePlanRunner("short", symbol="ETH_USDT", size_usd=250)}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            r.run_once()
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["action"], "open_short")
            self.assertEqual(payload["symbol"], "ETH_USDT")
            self.assertEqual(float(payload["size_usd"]), 250)
            self.assertIn("meta", payload)
            self.assertEqual(payload["meta"]["group"], "g")
            self.assertIn("votes", payload["meta"])
            self.assertEqual(payload["label"], "persona-g")


# ─────────────────────────────────────────────────────
# 8. schema 兼容性：persona action 必须被 parse_signal 接受
# ─────────────────────────────────────────────────────
class TestSchemaCompat(unittest.TestCase):
    """persona 产出的信号必须能被 omnialpha.schema.parse_signal 消费。"""

    def test_normalize_action_close_long(self):
        a, side = PersonaRunner._normalize_action("close_long")
        self.assertEqual(a, "close")
        self.assertEqual(side, "long")

    def test_normalize_action_close_short(self):
        a, side = PersonaRunner._normalize_action("close_short")
        self.assertEqual(a, "close")
        self.assertEqual(side, "short")

    def test_normalize_action_modify_tp_sl_passthrough(self):
        """modify_tp_sl 是 schema 合法动作，不再降级成 hold（2026-10-02 修）。

        此前它被映射成 hold，导致人格想调整止盈止损时**静默什么都不做**。
        """
        a, side = PersonaRunner._normalize_action("modify_tp_sl")
        self.assertEqual(a, "modify_tp_sl")
        self.assertIsNone(side)

    def test_normalize_action_passthrough(self):
        for a in ("open_long", "open_short", "close", "close_all", "reduce_long",
                  "reduce_short", "hold", "flatten"):
            na, side = PersonaRunner._normalize_action(a)
            self.assertEqual(na, a)
            self.assertIsNone(side)

    def _exec_and_parse(self, decision, action, extra_bot_kw=None):
        """执行 persona 一轮，然后用 schema 解析落盘信号。"""
        from omnialpha.schema import parse_signal
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"], "group": "g"})
            g = _group(target_account="a", topology="single_account")
            runners = {"a": FakePlanRunner(decision, action=action, **(extra_bot_kw or {})),
                        "b": FakePlanRunner(decision, action=action, **(extra_bot_kw or {}))}
            r = PersonaRunner(root, g, {"a": None, "b": None}, runners)
            res = r.run_once()
            if not res.get("executed"):
                return None, res
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertTrue(inbox, "signal should be written")
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            signal = parse_signal(payload, default_label="persona-test")
            return payload, signal

    def test_signal_open_long_schema_ok(self):
        payload, signal = self._exec_and_parse("long", "open_long",
                                               {"size_usd": 100, "confidence": 0.9})
        self.assertIsNotNone(signal)
        self.assertEqual(signal.intents[0].action, "open_long")

    def test_signal_close_long_schema_ok(self):
        payload, signal = self._exec_and_parse("close", "close_long")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.intents[0].action, "close")
        self.assertEqual(signal.intents[0].side, "long")

    def test_signal_close_short_schema_ok(self):
        payload, signal = self._exec_and_parse("close", "close_short")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.intents[0].action, "close")
        self.assertEqual(signal.intents[0].side, "short")

    def test_signal_modify_tp_sl_schema_ok(self):
        """modify_tp_sl 降级为 hold，schema 可解析。"""
        payload, signal = self._exec_and_parse("modify", "modify_tp_sl")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.intents[0].action, "hold")

    def test_signal_reduce_long_schema_ok(self):
        payload, signal = self._exec_and_parse("reduce", "reduce_long")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.intents[0].action, "close")

    def test_signal_flatten_schema_ok(self):
        payload, signal = self._exec_and_parse("close", "flatten")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.intents[0].action, "close_all")

    def test_all_persona_actions_schema_valid(self):
        """穷举 persona 可能产出的 action，确认全部可被 schema 解析。"""
        from omnialpha.schema import parse_signal
        actions = ["open_long", "open_short", "close", "close_all", "flatten",
                   "close_long", "close_short", "reduce_long", "reduce_short",
                   "modify_tp_sl", "hold", "add_long", "add_short",
                   "stop_entry_long", "stop_entry_short"]
        for act in actions:
            norm, side = PersonaRunner._normalize_action(act)
            payload = {"action": norm, "symbol": "BTC_USDT",
                       "size_usd": 50, "label": "persona-t"}
            if side:
                payload["side"] = side
            if norm in ("stop_entry_long", "stop_entry_short"):
                payload["trigger_price"] = 50000.0
            if norm == "modify_tp_sl":
                # modify_tp_sl 按设计要求至少一个目标价（executor 会校验）
                payload["tp"] = 60000.0
            try:
                sig = parse_signal(payload, default_label="t")
                self.assertTrue(sig.intents, f"no intents for {act}")
            except Exception as e:
                self.fail(f"action {act!r} (norm={norm!r}) failed schema: {e}")


if __name__ == "__main__":
    unittest.main()
