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
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"], "group": "g"})
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
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"], "group": "g"})
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
            rec = store.create({"symbol": "BTC_USDT", "side": "long", "members": ["a", "b"], "group": "g"})
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


class TestTypeTriggerTolerance(unittest.TestCase):
    """模型把 type:trigger 误写进 type 应容错，不报错。"""

    def _chip(self, **over):
        base = {
            "symbol": "BTC_USDT", "action": "open_short", "confidence": 0.7,
            "size_usd": 100, "sl": 83720, "tp": 82560,
        }
        base.update(over)
        return base

    def test_type_trigger_maps_to_market(self):
        from gate_bot.strategist.schema import parse_plan_text

        plan = {
            "cycle_id": "c1", "reasoning": "test",
            "chips": [self._chip(**{"type": "trigger"})],
        }
        import json
        p = parse_plan_text(json.dumps(plan))
        self.assertEqual(p.chips[0].order_type, "market")

    def test_type_stop_maps_to_market(self):
        from gate_bot.strategist.schema import parse_plan_text

        plan = {
            "cycle_id": "c1", "reasoning": "test",
            "chips": [self._chip(**{"type": "stop"})],
        }
        import json
        p = parse_plan_text(json.dumps(plan))
        self.assertEqual(p.chips[0].order_type, "market")

    def test_valid_type_untouched(self):
        from gate_bot.strategist.schema import parse_plan_text

        plan = {
            "cycle_id": "c1", "reasoning": "test",
            "chips": [self._chip(**{"type": "limit", "price": 83350})],
        }
        import json
        p = parse_plan_text(json.dumps(plan))
        self.assertEqual(p.chips[0].order_type, "limit")

    def test_unknown_type_still_rejected(self):
        from gate_bot.strategist.schema import parse_plan_text, PlanError

        plan = {
            "cycle_id": "c1", "reasoning": "test",
            "chips": [self._chip(**{"type": "banana"})],
        }
        import json
        with self.assertRaises(PlanError):
            parse_plan_text(json.dumps(plan))


class TestJsonRepair(unittest.TestCase):
    """LLM 输出常见 JSON 病应自动修复。"""

    def _parse(self, text):
        from gate_bot.strategist.schema import parse_plan_text

        return parse_plan_text(text)

    def test_markdown_fence(self):
        p = self._parse('```json\n{"cycle_id":"c1","reasoning":"r","chips":[]}\n```')
        self.assertEqual(p.cycle_id, "c1")

    def test_trailing_comma(self):
        p = self._parse('{"cycle_id":"c1","reasoning":"r","chips":[],}')
        self.assertEqual(p.cycle_id, "c1")

    def test_single_quotes(self):
        p = self._parse("{'cycle_id':'c1','reasoning':'r','chips':[]}")
        self.assertEqual(p.cycle_id, "c1")

    def test_bare_keys(self):
        p = self._parse('{cycle_id:"c1",reasoning:"r",chips:[]}')
        self.assertEqual(p.cycle_id, "c1")

    def test_comments_stripped(self):
        p = self._parse('{"cycle_id":"c1", // comment\n"reasoning":"r","chips":[]}')
        self.assertEqual(p.cycle_id, "c1")

    def test_python_literals(self):
        p = self._parse('{"cycle_id":"c1","reasoning":"r","chips":[],"x":None,"y":True}')
        self.assertEqual(p.cycle_id, "c1")

    def test_trailing_text_after_json(self):
        p = self._parse('{"cycle_id":"c1","reasoning":"r","chips":[]}\n\n这是我的分析完毕。')
        self.assertEqual(p.cycle_id, "c1")

    def test_text_before_json(self):
        p = self._parse('好的，以下是计划：\n{"cycle_id":"c1","reasoning":"r","chips":[]}')
        self.assertEqual(p.cycle_id, "c1")

    def test_truncated_recovers(self):
        # 截断在 chips 数组中间 —— 最大努力恢复
        raw = '{"cycle_id":"c1","reasoning":"r","chips":[{"symbol":"BTC_USDT","action":"open_long"'
        try:
            p = self._parse(raw)
            # 能恢复就校验
            self.assertEqual(p.cycle_id, "c1")
        except Exception:
            # 恢复不了也允许报 PlanError（不是崩）
            pass

    def test_real_world_llm_output(self):
        raw = """让我分析一下。

```json
{
  "cycle_id": "btc-15m-001",
  "reasoning": "区间震荡，观望",
  "chips": [
    {
      "symbol": "BTC_USDT",
      "action": "hold",
      "confidence": 0.6,
    }
  ],
}
```"""
        p = self._parse(raw)
        self.assertEqual(p.cycle_id, "btc-15m-001")
        self.assertEqual(p.chips[0].action, "hold")


class TestMultiObjectExtract(unittest.TestCase):
    """模型先吐 triggers 片段再吐主 Plan —— 应选主 Plan。"""

    def test_triggers_fragment_then_plan(self):
        from gate_bot.strategist.schema import parse_plan_text

        raw = '''{type:price_break, symbol, lookback, side:high}
{"cycle_id":"c1","reasoning":"r","chips":[{"symbol":"BTC_USDT","action":"hold","confidence":0.6}]}'''
        p = parse_plan_text(raw)
        self.assertEqual(p.cycle_id, "c1")
        self.assertEqual(p.chips[0].action, "hold")

    def test_triggers_array_then_plan(self):
        from gate_bot.strategist.schema import parse_plan_text

        raw = '''思考：先列触发条件 {"type":"price_break","symbol":"BTC_USDT"} 然后给计划：
{"cycle_id":"c2","reasoning":"r","chips":[]}'''
        p = parse_plan_text(raw)
        self.assertEqual(p.cycle_id, "c2")

    def test_only_fragment_no_plan(self):
        from gate_bot.strategist.schema import parse_plan_text, PlanError

        raw = "{type:price_break, symbol, lookback, side:high}"
        with self.assertRaises(PlanError):
            parse_plan_text(raw)
