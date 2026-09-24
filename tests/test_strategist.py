import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.schema import PlanError, parse_plan, parse_plan_text  # noqa: E402
from gate_bot.strategist.risk import RiskConfig, apply_risk  # noqa: E402
from gate_bot.strategist.bridge import chips_to_signal, write_signal_file  # noqa: E402
from gate_bot.strategist.llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402
from gate_bot.schema import parse_signal  # noqa: E402


class TestPlanSchema(unittest.TestCase):
    def test_parse_plan_ok(self):
        plan = parse_plan(
            {
                "cycle_id": "c1",
                "reasoning": "x",
                "chips": [
                    {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 50, "sl": 1},
                    {"symbol": "ETH_USDT", "action": "hold", "confidence": 0.5},
                ],
            }
        )
        self.assertEqual(len(plan.chips), 2)
        self.assertEqual(plan.chips[0].action, "open_long")

    def test_parse_plan_text_fenced(self):
        plan = parse_plan_text('```json\n{"chips":[{"symbol":"BTC_USDT","action":"hold"}]}\n```')
        self.assertEqual(plan.chips[0].action, "hold")

    def test_parse_plan_bad_action(self):
        with self.assertRaises(PlanError):
            parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "yolo"}]})

    def test_parse_plan_not_json(self):
        with self.assertRaises(PlanError):
            parse_plan_text("not json")

    def test_confidence_scale_100(self):
        plan = parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 85}]})
        self.assertAlmostEqual(plan.chips[0].confidence, 0.85)


class TestRisk(unittest.TestCase):
    def _plan(self):
        return parse_plan(
            {
                "chips": [
                    {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 50},
                    {"symbol": "ETH_USDT", "action": "open_short", "confidence": 0.6, "size_usd": 50},
                    {"symbol": "SOL_USDT", "action": "open_long", "confidence": 0.95, "size_usd": 50},
                    {"symbol": "XRP_USDT", "action": "close", "confidence": 0.99},
                ]
            }
        )

    def test_min_confidence_hold(self):
        r = apply_risk(self._plan(), RiskConfig(min_confidence=0.8, max_chips=10))
        acts = {c.symbol: c.action for c in r.accepted}
        self.assertEqual(acts.get("ETH_USDT"), "hold")

    def test_max_notional_truncate(self):
        plan = parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 100}]})
        r = apply_risk(plan, RiskConfig(min_confidence=0.5, max_notional_usd=40))
        self.assertEqual(r.accepted[0].size_usd, 40)

    def test_allow_actions(self):
        r = apply_risk(self._plan(), RiskConfig(min_confidence=0.5, allow_actions={"hold", "close"}))
        actions = {c.action for c in r.accepted}
        self.assertNotIn("open_long", actions)

    def test_max_chips(self):
        r = apply_risk(self._plan(), RiskConfig(min_confidence=0.5, max_chips=1))
        tradeable = [c for c in r.accepted if c.action != "hold"]
        self.assertLessEqual(len(tradeable), 1)

    def test_open_requires_size(self):
        plan = parse_plan({"chips": [{"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9}]})
        r = apply_risk(plan, RiskConfig(min_confidence=0.5))
        self.assertTrue(all(c.action == "hold" for c in r.accepted))


class TestBridge(unittest.TestCase):
    def test_bridge_to_parse_signal(self):
        plan = parse_plan(
            {
                "cycle_id": "c9",
                "chips": [
                    {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 30, "sl": 1},
                    {"symbol": "ETH_USDT", "action": "hold", "confidence": 0.2},
                ],
            }
        )
        risk = apply_risk(plan, RiskConfig(min_confidence=0.75, max_notional_usd=50))
        payload = chips_to_signal(plan, risk, bot_id="alpha")
        self.assertTrue(payload.get("orders"))
        sig = parse_signal(payload)  # must be executable schema
        self.assertTrue(sig.intents)

    def test_write_hold_when_empty(self):
        plan = parse_plan({"cycle_id": "c0", "chips": [{"symbol": "BTC_USDT", "action": "hold"}]})
        risk = apply_risk(plan, RiskConfig(min_confidence=0.9))
        payload = chips_to_signal(plan, risk)
        with tempfile.TemporaryDirectory() as td:
            p = write_signal_file(Path(td), payload, cycle_id="c0")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data.get("action"), "hold")


class TestLLMClient(unittest.TestCase):
    def test_missing_key(self):
        import os

        os.environ.pop("OPENAI_API_KEY", None)
        client = LLMClient(LLMConfig(api_key_env="OPENAI_API_KEY_MISSING_TEST"))
        with self.assertRaises(LLMError):
            client.chat("s", "u")

    def test_parse_response(self):
        import os
        import urllib.request

        os.environ["OPENAI_API_KEY"] = "k"
        client = LLMClient(LLMConfig(api_key_env="OPENAI_API_KEY"))

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{\"chips\":[]}"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        old = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: FakeResp()
        try:
            self.assertEqual(client.chat("s", "u"), '{"chips":[]}')
        finally:
            urllib.request.urlopen = old
            os.environ.pop("OPENAI_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
