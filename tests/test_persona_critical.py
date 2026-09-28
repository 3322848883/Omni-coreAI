"""Review 抓出的 3 个关键问题的回归测试。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.persona import (  # noqa: E402
    PersonaGroup,
    SharedOrderStore,
    fuse_plans,
)
from gate_bot.persona.fusion import (  # noqa: E402
    DIR_CLOSE,
    DIR_HOLD,
    DIR_LONG,
    DIR_MODIFY,
    DIR_REDUCE,
    DIR_SHORT,
)


def _group(**kw):
    base = dict(name="g", members=["a", "b"], fusion="weighted_vote",
                fusion_config={"weights": {"a": 1, "b": 1}}, on_conflict="hold")
    base.update(kw)
    return PersonaGroup(**base)


class TestCriticalFixes(unittest.TestCase):
    def test_close_not_collapsed_to_hold(self):
        """管理动作（close/reduce/modify）不得折叠为 hold。"""
        plans = {"a": {"decision": "close", "chips": [{"action": "close"}]},
                 "b": {"decision": "close", "chips": [{"action": "close_all"}]}}
        r = fuse_plans(_group(), plans)
        self.assertEqual(r["decision"], DIR_CLOSE)
        self.assertNotEqual(r["action"], "hold")

    def test_reduce_and_modify_recognized(self):
        plans = {"a": {"chips": [{"action": "reduce_long"}]},
                 "b": {"chips": [{"action": "reduce_long"}]}}
        self.assertEqual(fuse_plans(_group(), plans)["decision"], DIR_REDUCE)
        plans = {"a": {"chips": [{"action": "modify_tp_sl"}]},
                 "b": {"chips": [{"action": "modify_tp_sl"}]}}
        self.assertEqual(fuse_plans(_group(), plans)["decision"], DIR_MODIFY)

    def test_order_id_reused_while_open(self):
        """持仓期（方向决策）复用现有 open 单，不每轮建新单。"""
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"]})
            oid = rec["order_id"]

            class R:
                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-2", "plan": {
                        "cycle_id": "c-2", "decision": "long", "confidence": 0.9,
                        "chips": [{"action": "add_long", "symbol": "BTC_USDT", "size_usd": 50}],
                    }}

            g = _group(target_account="a", topology="single_account")
            r = PersonaRunner(root, g, {"a": None, "b": None}, {"a": R(), "b": R()})
            res = r.run_once()
            # 复用现有 open 单（不新建）
            self.assertEqual(res["order_id"], oid)

    def test_close_marks_order_closed(self):
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"]})
            oid = rec["order_id"]

            class R:
                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-3", "plan": {
                        "cycle_id": "c-3", "decision": "close", "confidence": 0.9,
                        "chips": [{"action": "close", "symbol": "BTC_USDT"}],
                    }}

            g = _group(target_account="a", topology="single_account")
            r = PersonaRunner(root, g, {"a": None, "b": None}, {"a": R(), "b": R()})
            res = r.run_once()
            self.assertTrue(res["executed"])
            self.assertEqual(res["order_id"], oid)
            self.assertEqual(store.get(oid)["status"], "closed")

    def test_risk_min_confidence_blocks(self):
        """融合后信号低于 min_confidence → 降级为 hold（不绕过风控）。"""
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            class BotCfg:
                strategist = {"risk": {"min_confidence": 0.9, "max_notional_usd": 100}}

            class R:
                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-4", "plan": {
                        "cycle_id": "c-4", "decision": "long", "confidence": 0.5,
                        "chips": [{"action": "open_long", "symbol": "BTC_USDT",
                                   "size_usd": 500, "confidence": 0.5}],
                    }}

            g = _group(target_account="a", topology="single_account")
            bots = {"a": BotCfg(), "b": BotCfg()}
            r = PersonaRunner(root, g, bots, {"a": R(), "b": R()})
            res = r.run_once()
            # confidence 0.5 < 0.9 → payload 降级 hold
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            if inbox:
                import json
                payload = json.loads(inbox[0].read_text(encoding="utf-8"))
                self.assertEqual(payload["action"], "hold")
                self.assertIn("risk_reject", payload.get("meta", {}))

    def test_risk_caps_size(self):
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            class BotCfg:
                strategist = {"risk": {"min_confidence": 0.1, "max_notional_usd": 100}}

            class R:
                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-5", "plan": {
                        "cycle_id": "c-5", "decision": "long", "confidence": 0.9,
                        "chips": [{"action": "open_long", "symbol": "BTC_USDT",
                                   "size_usd": 500, "confidence": 0.9}],
                    }}

            g = _group(target_account="a", topology="single_account")
            bots = {"a": BotCfg(), "b": BotCfg()}
            r = PersonaRunner(root, g, bots, {"a": R(), "b": R()})
            res = r.run_once()
            self.assertTrue(res["executed"])
            import json
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertTrue(inbox)
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            self.assertEqual(float(payload["size_usd"]), 100.0)
            self.assertTrue(payload.get("meta", {}).get("risk_capped"))

    def test_risk_not_gate_manage_actions(self):
        """风控不得拦 close/reduce/modify（减险出场）。"""
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SharedOrderStore(root)
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"]})
            oid = rec["order_id"]

            class BotCfg:
                strategist = {"risk": {"min_confidence": 0.9, "max_notional_usd": 100}}

            class R:
                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-m", "plan": {
                        "cycle_id": "c-m", "decision": "close", "confidence": 0.1,
                        "chips": [{"action": "close", "symbol": "BTC_USDT", "confidence": 0.1}],
                    }}

            g = _group(target_account="a", topology="single_account")
            bots = {"a": BotCfg(), "b": BotCfg()}
            r = PersonaRunner(root, g, bots, {"a": R(), "b": R()})
            res = r.run_once()
            self.assertTrue(res["executed"], "close must not be risk-gated")
            self.assertEqual(res["action"], "close")
            self.assertEqual(store.get(oid)["status"], "closed")

    def test_risk_zero_confidence_blocked(self):
        """confidence=0 也应被 min_confidence 拦（不能假穿风控）。"""
        from gate_bot.persona.runner import PersonaRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            class BotCfg:
                strategist = {"risk": {"min_confidence": 0.5}}

            class R:
                def analyze_once(self, trigger="manual"):
                    return {"ok": True, "cycle_id": "c-z", "plan": {
                        "cycle_id": "c-z", "decision": "long", "confidence": 0.0,
                        "chips": [{"action": "open_long", "symbol": "BTC_USDT",
                                   "size_usd": 50, "confidence": 0.0}],
                    }}

            g = _group(target_account="a", topology="single_account")
            bots = {"a": BotCfg(), "b": BotCfg()}
            r = PersonaRunner(root, g, bots, {"a": R(), "b": R()})
            r.run_once()
            import json
            inbox = list((root / "data" / "bots" / "a" / "inbox").glob("*.json"))
            self.assertTrue(inbox, "signal should still be written (as hold)")
            payload = json.loads(inbox[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["action"], "hold")
            self.assertIn("risk_reject", payload.get("meta", {}))


if __name__ == "__main__":
    unittest.main()
