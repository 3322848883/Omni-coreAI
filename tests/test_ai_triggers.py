import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.trigger_store import (  # noqa: E402
    AITriggerPolicy,
    AITriggerStore,
    TriggerPolicyError,
    validate_trigger_payload,
)
from omnialpha.strategist.schema import parse_plan  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402


class TestTriggerPolicy(unittest.TestCase):
    def test_reject_unknown_type(self):
        with self.assertRaises(TriggerPolicyError):
            validate_trigger_payload(
                {"type": "evil", "symbol": "BTC_USDT"},
                AITriggerPolicy(enabled=True),
                ["BTC_USDT"],
            )

    def test_reject_bad_symbol(self):
        with self.assertRaises(TriggerPolicyError):
            validate_trigger_payload(
                {"type": "price_break", "symbol": "DOGE_USDT", "lookback": 20},
                AITriggerPolicy(enabled=True),
                ["BTC_USDT"],
            )

    def test_limit_clamp_reject(self):
        with self.assertRaises(TriggerPolicyError):
            validate_trigger_payload(
                {"type": "price_break", "symbol": "BTC_USDT", "lookback": 9999},
                AITriggerPolicy(enabled=True),
                ["BTC_USDT"],
            )

    def test_store_add_ttl_and_max(self):
        with tempfile.TemporaryDirectory() as td:
            store = AITriggerStore(Path(td) / "ai_triggers.json", AITriggerPolicy(enabled=True, max_active=2))
            n1 = validate_trigger_payload(
                {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "gt", "level": 70},
                store.policy, ["BTC_USDT"])
            t = store.add(n1, ["BTC_USDT"])
            self.assertTrue(t.id.startswith("t-"))
            self.assertEqual(len(store.active()), 1)
            store.add(validate_trigger_payload(
                {"type": "price_break", "symbol": "BTC_USDT", "lookback": 20, "side": "low"},
                store.policy, ["BTC_USDT"]), ["BTC_USDT"])
            with self.assertRaises(TriggerPolicyError):
                store.add(validate_trigger_payload(
                    {"type": "atr_spike", "symbol": "BTC_USDT", "mult": 2},
                    store.policy, ["BTC_USDT"]), ["BTC_USDT"])
            # TTL expiry
            store._items[0].expire_at = time.time() - 1
            self.assertEqual(len(store.active()), 1)

    def test_plan_triggers_parse(self):
        plan = parse_plan({
            "chips": [{"symbol": "BTC_USDT", "action": "hold"}],
            "triggers": [{"type": "price_break", "symbol": "BTC_USDT", "lookback": 20, "side": "low"}],
        })
        self.assertEqual(len(plan.triggers), 1)
        self.assertEqual(plan.triggers[0]["type"], "price_break")

    def test_apply_ai_triggers_in_runner(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = StrategistConfig(
                symbols=["BTC_USDT"],
                ai_triggers={"enabled": True, "max_active": 3, "default_ttl_sec": 600},
            )
            runner = PlanRunner(
                client=object(), cfg=cfg,
                inbox=Path(td) / "inbox", history_dir=Path(td) / "hist",
                llm=object(),
            )
            notes = runner._apply_ai_triggers(parse_plan({
                "chips": [],
                "triggers": [{"type": "ema_cross", "symbol": "BTC_USDT", "fast": 9, "slow": 21, "dir": "down"}],
            }))
            self.assertTrue(any("ai_trigger_add" in n for n in notes))
            self.assertEqual(len(runner._ai_store.active()), 1)


if __name__ == "__main__":
    unittest.main()
