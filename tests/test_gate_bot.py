import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.gate_client import ContractMeta, GateApiError, resolve_symbol  # noqa: E402
from gate_bot.schema import (  # noqa: E402
    SchemaError,
    expand_signal,
    infer_trigger_rules,
    parse_signal,
)
from gate_bot.sizing import default_trigger_limit_price, usd_to_contracts  # noqa: E402
from gate_bot.executor import Executor  # noqa: E402
from gate_bot.watcher import ProjectPaths, process_file  # noqa: E402
from gate_bot.config import BotConfig  # noqa: E402


class TestSchema(unittest.TestCase):
    def test_single_open_long(self):
        sig = parse_signal(
            {
                "action": "open_long",
                "symbol": "BTC",
                "size_usd": 100,
                "tp": 75000,
                "sl": 72000,
            }
        )
        self.assertEqual(len(sig.intents), 1)
        intent = sig.intents[0]
        self.assertEqual(intent.symbol, "BTC_USDT")
        self.assertEqual(intent.action, "open_long")
        self.assertEqual(intent.trigger_rule_tp, 1)
        self.assertEqual(intent.trigger_rule_sl, 2)

    def test_open_short_rules_inverted(self):
        self.assertEqual(infer_trigger_rules("open_short", True), 2)
        self.assertEqual(infer_trigger_rules("open_short", False), 1)

    def test_orders_array(self):
        sig = parse_signal(
            {
                "orders": [
                    {"action": "open_long", "symbol": "ETH_USDT", "size_usd": 50, "type": "limit", "price": 3000},
                    {"action": "hold", "symbol": "ETH_USDT"},
                ]
            }
        )
        self.assertEqual(len(sig.intents), 2)

    def test_grid_expands(self):
        sig = parse_signal(
            {
                "action": "grid",
                "symbol": "BTC_USDT",
                "side": "long",
                "levels": [
                    {"price": 70000, "size_usd": 50},
                    {"price": 69500, "size_usd": 50},
                ],
                "tp": 72000,
                "sl": 68000,
            }
        )
        intents = expand_signal(sig)
        self.assertEqual(len(intents), 2)
        self.assertEqual(intents[0].action, "open_long")
        self.assertEqual(intents[0].price, 70000)
        self.assertIsNone(intents[0].tp)  # tp/sl only on last level
        self.assertEqual(intents[1].tp, 72000)

    def test_reject_action_plus_orders(self):
        with self.assertRaises(SchemaError):
            parse_signal({"action": "hold", "orders": [{"action": "hold"}]})

    def test_reject_market_tp(self):
        with self.assertRaises(SchemaError):
            parse_signal(
                {
                    "action": "open_long",
                    "symbol": "BTC_USDT",
                    "size_usd": 10,
                    "tp": 1,
                    "tp_type": "market",
                }
            )

    def test_limit_requires_price(self):
        with self.assertRaises(SchemaError):
            parse_signal(
                {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 10, "type": "limit"}
            )

    def test_resolve_symbol(self):
        self.assertEqual(resolve_symbol("btc"), "BTC_USDT")
        self.assertEqual(resolve_symbol("XAU"), "XAU_USDT")


class TestSizing(unittest.TestCase):
    def meta(self):
        return ContractMeta(
            name="BTC_USDT",
            quanto_multiplier=0.0001,
            order_size_round=1,
            order_price_round=0.1,
            leverage_max=100,
        )

    def test_usd_to_contracts(self):
        # 100 USD / (50000 * 0.0001) = 100/5 = 20
        self.assertEqual(usd_to_contracts(100, 50000, self.meta()), 20)

    def test_too_small(self):
        with self.assertRaises(GateApiError):
            usd_to_contracts(1, 50000, self.meta())

    def test_default_trigger_limit(self):
        self.assertAlmostEqual(default_trigger_limit_price(100, "long", True), 99.9)
        self.assertAlmostEqual(default_trigger_limit_price(100, "short", True), 100.1)


class FakeClient:
    env = "testnet"

    def __init__(self):
        self.orders = []
        self.price_orders = []
        self.dual = False
        self.meta = ContractMeta("BTC_USDT", 0.0001, 1, 0.1, 100)

    def banner(self):
        return "[TEST]"

    def get_contract(self, symbol):
        return self.meta

    def get_last_price(self, symbol):
        return 50000

    def set_leverage(self, symbol, leverage):
        return {"ok": True}

    def set_margin_mode(self, symbol, mode):
        return {"ok": True}

    def is_dual_position_mode(self):
        return self.dual

    def get_position_mode(self):
        return "dual" if self.dual else "single"

    def place_order(self, body):
        self.orders.append(body)
        return {"id": len(self.orders), **body}

    def place_price_order(self, body):
        self.price_orders.append(body)
        return {"id": 100 + len(self.price_orders), **body}

    def close_position(self, contract, side=None, size=0):
        body = {"contract": contract, "side": side, "size": size, "reduce_only": True}
        self.orders.append(body)
        return {"id": len(self.orders), **body}

    def cancel_all_orders(self, contract):
        return {"cancelled": contract}

    def cancel_all_price_orders(self, contract=None):
        return {"cancelled": contract or "ALL"}

    def get_positions(self):
        return []


class TestExecutor(unittest.TestCase):
    def test_open_long_with_tp_sl(self):
        client = FakeClient()
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], max_notional_usd=500)
        sig = parse_signal(
            {
                "action": "open_long",
                "symbol": "BTC_USDT",
                "size_usd": 100,
                "tp": 55000,
                "sl": 48000,
            }
        )
        report = ex.execute_signal(sig)
        self.assertTrue(report.ok)
        self.assertEqual(len(client.orders), 1)
        self.assertEqual(client.orders[0]["size"], 20)  # 100/(50000*0.0001)
        self.assertEqual(len(client.price_orders), 2)
        # nested {initial, trigger}; long close-trigger side is short (negative size)
        tp = client.price_orders[0]
        sl = client.price_orders[1]
        self.assertEqual(tp["initial"]["size"], -20)
        self.assertTrue(tp["initial"]["reduce_only"])
        self.assertEqual(tp["trigger"]["rule"], 1)  # tp
        self.assertEqual(sl["trigger"]["rule"], 2)  # sl

    def test_notional_guard(self):
        client = FakeClient()
        ex = Executor(client, max_notional_usd=50)
        sig = parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size_usd": 100})
        report = ex.execute_signal(sig)
        self.assertFalse(report.ok)
        self.assertEqual(len(client.orders), 0)

    def test_close_dual_requires_side(self):
        client = FakeClient()
        client.dual = True
        ex = Executor(client)
        sig = parse_signal({"action": "close", "symbol": "BTC_USDT"})
        report = ex.execute_signal(sig)
        self.assertFalse(report.ok)


class TestWatcher(unittest.TestCase):
    def test_process_invalid_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = ProjectPaths(root)
            paths.ensure()
            bot = BotConfig(bot_id="alpha", env="testnet")
            inbox = paths.bot_inbox("alpha")
            f = inbox / "bad.json"
            f.write_text("{not json", encoding="utf-8")
            ok = process_file(f, bot, paths)
            self.assertFalse(ok)
            self.assertTrue((paths.bot_failed("alpha") / "bad.json").exists())
            self.assertTrue((paths.bot_failed("alpha") / "bad.json.error.json").exists())

    def test_process_hold_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = ProjectPaths(root)
            paths.ensure()
            bot = BotConfig(bot_id="alpha", env="testnet")

            class HoldClient(FakeClient):
                pass

            f = paths.bot_inbox("alpha") / "hold.json"
            f.write_text(json.dumps({"action": "hold"}), encoding="utf-8")
            ex = Executor(HoldClient())
            ok = process_file(f, bot, paths, executor=ex)
            self.assertTrue(ok)
            self.assertTrue((paths.bot_done("alpha") / "hold.json").exists())


if __name__ == "__main__":
    unittest.main()
