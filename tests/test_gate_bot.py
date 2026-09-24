import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.gate_client import (
    ContractMeta,
    GateApiError,
    GateClient,
    load_credentials,
    resolve_symbol,
)  # noqa: E402
from gate_bot.schema import (  # noqa: E402
    SchemaError,
    expand_signal,
    infer_trigger_rules,
    parse_signal,
)
from gate_bot.sizing import default_trigger_limit_price, pct_to_size_usd, usd_to_contracts  # noqa: E402
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

    def test_flatten_empty_is_noop_success(self):
        class FlatClient(FakeClient):
            def close_position(self, contract, side=None, size=0):
                raise GateApiError("400 POSITION_EMPTY", status=400, label="POSITION_EMPTY")

        ex = Executor(FlatClient())
        rep = ex.execute_signal(parse_signal({"action": "flatten", "symbol": "BTC_USDT"}))
        self.assertTrue(rep.ok)
        self.assertEqual(rep.results[0].detail.get("closed_order_ids"), [])

    def test_watch_alias_is_hold_no_exec(self):
        for a in ("hold", "watch", "skip", ""):
            sig = parse_signal({"action": a, "meta": {"reasoning": "x"}})
            self.assertEqual(sig.intents[0].action, "hold")
        client = FakeClient()
        report = Executor(client).execute_signal(parse_signal({"action": "watch"}))
        self.assertTrue(report.ok)
        self.assertEqual(len(client.orders), 0)
        self.assertTrue(report.results[0].detail.get("skipped"))

    def test_stop_entry_is_breakout_not_stoploss(self):
        sig = parse_signal({"action": "buy_stop", "symbol": "BTC_USDT", "size": 1, "trigger_price": 90000})
        self.assertEqual(sig.intents[0].action, "stop_entry_long")
        self.assertEqual(parse_signal(
            {"action": "sell_stop", "symbol": "BTC_USDT", "size": 1, "trigger_price": 80000}
        ).intents[0].action, "stop_entry_short")
        client = FakeClient()
        self.assertTrue(Executor(client).execute_signal(sig).ok)
        body = client.price_orders[0]
        self.assertEqual(body["initial"]["size"], 1)
        self.assertEqual(body["trigger"]["rule"], 1)
        self.assertNotIn("reduce_only", body["initial"])

    def test_add_reduce_logs_requested_action(self):
        client = FakeClient()
        ex = Executor(client, max_notional_usd=5000)
        rep = ex.execute_signal(parse_signal({
            "action": "add_long", "symbol": "BTC_USDT", "size": 1,
        }))
        self.assertTrue(rep.ok)
        self.assertEqual(rep.results[0].action, "add_long")
        self.assertEqual(rep.results[0].detail["executed_as"], "open_long")
        rep2 = ex.execute_signal(parse_signal({
            "action": "reduce_long", "symbol": "BTC_USDT", "size": 1,
        }))
        self.assertTrue(rep2.ok)
        self.assertEqual(rep2.results[0].action, "reduce_long")
        self.assertEqual(rep2.results[0].detail["executed_as"], "close")

    def test_replace_cancels_symbol_before_open(self):
        client = FakeClient()
        ex = Executor(client, symbols_whitelist=["BTC_USDT", "ETH_USDT"])
        rep = ex.execute_signal(parse_signal({
            "replace": True,
            "action": "open_long",
            "symbol": "BTC_USDT",
            "size": 1,
        }))
        self.assertTrue(rep.ok)
        kinds = [r.action for r in rep.results]
        self.assertIn("replace_cancel", kinds)
        self.assertEqual(client.cancelled.count("BTC_USDT"), 1)  # orders
        # price_orders cancel uses ("trail", ...) only for trail; cancel_all_price_orders
        self.assertEqual(kinds[0], "replace_cancel")
        self.assertEqual(kinds[1], "open_long")

    def test_replace_all_cancels_whitelist(self):
        client = FakeClient()
        ex = Executor(client, symbols_whitelist=["BTC_USDT", "ETH_USDT"])
        rep = ex.execute_signal(parse_signal({
            "replace": "all",
            "orders": [
                {"action": "hold"},
            ],
        }))
        # hold still runs after replace
        self.assertTrue(rep.ok)
        self.assertIn("BTC_USDT", client.cancelled)
        self.assertIn("ETH_USDT", client.cancelled)

    def test_replace_none_no_cancel(self):
        client = FakeClient()
        ex = Executor(client)
        rep = ex.execute_signal(parse_signal({"action": "add_long", "symbol": "BTC_USDT", "size": 1}))
        self.assertTrue(rep.ok)
        self.assertEqual(client.cancelled, [])

    def test_position_policy_strict_blocks_entry(self):
        client = FakeClient()
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 2, "mode": "single"}]
        ex = Executor(client, position_policy="strict")
        rep = ex.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size": 1}))
        self.assertFalse(rep.ok)
        self.assertIn("POSITION_EXISTS", rep.results[0].error)
        # same-side add allowed
        rep2 = ex.execute_signal(parse_signal({"action": "add_long", "symbol": "BTC_USDT", "size": 1}))
        self.assertTrue(rep2.ok)
        # reduce allowed
        rep3 = ex.execute_signal(parse_signal({"action": "reduce_long", "symbol": "BTC_USDT", "size": 1}))
        self.assertTrue(rep3.ok)

    def test_position_policy_free_allows_entry(self):
        client = FakeClient()
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 2, "mode": "single"}]
        ex = Executor(client, position_policy="free")
        rep = ex.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size": 1}))
        self.assertTrue(rep.ok)

    def test_limit_order_tpsl_resting_reduce_only(self):
        client = FakeClient()
        ex = Executor(client)
        sig = parse_signal({
            "action": "open_long",
            "symbol": "BTC_USDT",
            "size": 1,
            "type": "market",
            "tp": 55000,
            "sl": 48000,
            "tp_mode": "limit_order",
            "sl_mode": "limit_order",
            "label": "ex",
        })
        report = ex.execute_signal(sig)
        self.assertTrue(report.ok)
        # 1 entry + 2 reduce_only limits; no price_orders (not conditional)
        self.assertEqual(len(client.orders), 3)
        self.assertEqual(len(client.price_orders), 0)
        tp, sl = client.orders[1], client.orders[2]
        self.assertTrue(tp.get("reduce_only"))
        self.assertEqual(tp.get("tif"), "gtc")
        self.assertEqual(tp.get("size"), -1)  # sell to close long
        self.assertTrue(sl.get("reduce_only"))
        self.assertEqual(sl.get("size"), -1)

    def test_default_tpsl_stays_trigger(self):
        client = FakeClient()
        ex = Executor(client)
        ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size": 1,
            "tp": 55000, "sl": 48000,
        }))
        self.assertEqual(len(client.orders), 1)
        self.assertEqual(len(client.price_orders), 2)

    def test_empty_action_is_hold(self):
        self.assertEqual(parse_signal({"symbol": "BTC_USDT"}).intents[0].action, "hold")
        self.assertEqual(parse_signal({"action": ""}).intents[0].action, "hold")

    def test_empty_orders_is_hold(self):
        self.assertEqual(parse_signal({"orders": []}).intents[0].action, "hold")

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
        self.assertIsNone(intents[0].tp)
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

    def test_size_pct_and_margin_pct(self):
        sig = parse_signal(
            {"action": "open_long", "symbol": "BTC_USDT", "size_pct": 0.1}
        )
        self.assertEqual(sig.intents[0].size_pct, 0.1)
        with self.assertRaises(SchemaError):
            parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size_pct": 1.5})
        with self.assertRaises(SchemaError):
            parse_signal({"action": "open_long", "symbol": "BTC_USDT"})

    def test_trail_action(self):
        sig = parse_signal(
            {
                "action": "trail",
                "symbol": "BTC_USDT",
                "amount": -2,
                "price_offset": "0.5%",
                "activation_price": "0",
            }
        )
        intent = sig.intents[0]
        self.assertEqual(intent.action, "trail")
        self.assertEqual(intent.size, 2)
        self.assertEqual(intent.side, "short")
        self.assertEqual(intent.price_offset, "0.5%")

    def test_pct_to_size_usd(self):
        self.assertAlmostEqual(pct_to_size_usd(0.1, 1000), 100)
        with self.assertRaises(GateApiError):
            pct_to_size_usd(0, 1000)

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
        self.cancelled = []

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

    def get_order(self, order_id):
        return {"id": int(order_id) if str(order_id).isdigit() else order_id, "status": "open", "left": 1}

    def get_price_order(self, price_order_id):
        return {"id": price_order_id, "status": "open"}

    def close_position(self, contract, side=None, size=0):
        body = {"contract": contract, "side": side, "size": size, "reduce_only": True}
        self.orders.append(body)
        return {"id": len(self.orders), **body}

    def list_orders(self, contract=None):
        return []

    def cancel_all_orders(self, contract):
        self.cancelled.append(contract)
        return {"cancelled": contract}

    def cancel_all_price_orders(self, contract=None):
        return {"cancelled": contract or "ALL"}

    def get_positions(self):
        return []

    def get_available_usdt(self):
        return 1000.0

    def place_trailing_order(self, body):
        self.price_orders.append(body)
        return {"id": 200 + len(self.price_orders), **body}

    def stop_trailing_orders(self, contract=None):
        self.cancelled.append(("trail", contract))
        return {"cancelled": contract or "ALL"}


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
        self.assertEqual(client.orders[0]["size"], 20)
        self.assertEqual(len(client.price_orders), 2)
        tp = client.price_orders[0]
        sl = client.price_orders[1]
        self.assertEqual(tp["initial"]["size"], -20)
        self.assertTrue(tp["initial"]["reduce_only"])
        self.assertEqual(tp["trigger"]["rule"], 1)
        self.assertEqual(sl["trigger"]["rule"], 2)

    def test_notional_guard(self):
        client = FakeClient()
        ex = Executor(client, max_notional_usd=50)
        report = ex.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size_usd": 100}))
        self.assertFalse(report.ok)
        self.assertEqual(len(client.orders), 0)

    def test_close_dual_requires_side(self):
        client = FakeClient()
        client.dual = True
        ex = Executor(client)
        report = ex.execute_signal(parse_signal({"action": "close", "symbol": "BTC_USDT"}))
        self.assertFalse(report.ok)

    def test_dual_close_long_sells_negative_size(self):
        client = GateClient("k", "s", env="live")
        client.is_dual_position_mode = lambda: True
        client.get_positions = lambda: [
            {"contract": "BTC_USDT", "size": 5, "mode": "dual_long"},
            {"contract": "BTC_USDT", "size": -2, "mode": "dual_short"},
        ]
        placed = {}
        client.place_order = lambda body: placed.update(body) or {"id": 1, **body}
        client.close_position("BTC_USDT", side="long", size=0)
        self.assertEqual(placed["size"], -5)
        self.assertTrue(placed["reduce_only"])

    def test_cancel_all_without_symbol_uses_open_orders(self):
        client = FakeClient()
        client.list_orders = lambda contract=None: [
            {"contract": "BTC_USDT"},
            {"contract": "ETH_USDT"},
            {"contract": "BTC_USDT"},
        ]
        ex = Executor(client)
        self.assertTrue(ex.execute_signal(parse_signal({"action": "cancel_all"})).ok)
        self.assertEqual(client.cancelled, ["BTC_USDT", "ETH_USDT"])


    def test_open_size_pct(self):
        client = FakeClient()
        ex = Executor(client, max_notional_usd=5000)
        sig = parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size_pct": 0.1})
        # 10% of 1000 available = 100 USD → 20 contracts at 50000
        report = ex.execute_signal(sig)
        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(client.orders[0]["size"], 20)

    def test_trail_places_trailing_order(self):
        client = FakeClient()
        ex = Executor(client)
        sig = parse_signal(
            {
                "action": "trail",
                "symbol": "BTC_USDT",
                "amount": -3,
                "price_offset": "0.5",
            }
        )
        report = ex.execute_signal(sig)
        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(client.price_orders[0]["amount"], "-3")
        self.assertEqual(client.price_orders[0]["price_offset"], "0.5")


class TestGateClient(unittest.TestCase):
    def test_sign_headers_and_position_mode(self):
        client = GateClient("KEY123", "SECRET456", env="live")
        self.assertEqual(client.base, "https://api.gateio.ws")
        self.assertIn("LIVE", client.banner())
        captured = {}

        class FakeResp:
            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=15):
            captured["headers"] = {k.title(): v for k, v in req.header_items()}
            captured["method"] = req.get_method()
            return FakeResp()

        import urllib.request as ur

        old = ur.urlopen
        ur.urlopen = fake_urlopen
        try:
            self.assertEqual(client.rest_signed_request("GET", "/x"), {"ok": True})
        finally:
            ur.urlopen = old
        self.assertEqual(captured["headers"].get("Key"), "KEY123")
        self.assertTrue(captured["headers"].get("Sign"))
        self.assertEqual(captured["method"], "GET")

        client.get_account = lambda: {"position_mode": "dual_long_short"}
        self.assertTrue(client.is_dual_position_mode())
        client.public_get = lambda path, qs="": [
            {
                "name": "BTC_USDT",
                "quanto_multiplier": 0.0001,
                "order_size_round": 1,
                "order_price_round": 0.1,
                "leverage_max": 100,
            }
        ]
        self.assertEqual(client.get_contract("BTC_USDT").quanto_multiplier, 0.0001)
        with self.assertRaises(GateApiError):
            client.get_contract("NOPE_USDT")

    def test_official_paths_and_close_and_trail(self):
        client = GateClient("k", "s", env="testnet")
        calls = []

        def fake(method, path, qs="", body=None):
            calls.append((method, path, qs, body))
            if path.endswith("/accounts"):
                return {"position_mode": "dual", "in_dual_mode": True, "available": "10", "total": "10"}
            if path.endswith("/leverage"):
                return [{"leverage": "5"}]
            return {"id": 1}

        client.rest_signed_request = fake
        client._position_mode_cache = (None, 0.0)
        self.assertEqual(client.get_position_mode(), "dual")
        self.assertTrue(any(c[1].endswith("/accounts") for c in calls))
        client.set_leverage("BTC_USDT", 5)
        lev = [c for c in calls if c[1].endswith("/leverage")][-1]
        self.assertIn("dual_comp", lev[1])
        self.assertEqual(lev[2], "leverage=5")

        single = GateClient("k", "s", env="live")
        single.is_dual_position_mode = lambda: False
        placed = {}
        single.place_order = lambda body: placed.update(body) or {"id": 1, **body}
        single.close_position("BTC_USDT", size=0)
        self.assertTrue(placed.get("close") and placed.get("reduce_only"))
        self.assertEqual(placed.get("size"), 0)

        client.rest_signed_request = lambda *a, **k: {"code": -1, "message": "InvalidRequest"}
        with self.assertRaises(GateApiError):
            client.place_trailing_order({"contract": "BTC_USDT"})

    def test_market_slippage_fallback(self):
        client = GateClient("k", "s", env="testnet")

        def fake(method, path, qs="", body=None):
            if body and body.get("price") == "0":
                raise GateApiError("slip", status=400, label="MARKET_PRICE_TOO_DEVIATED")
            return {"id": 9, "price": body.get("price")}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        result = client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(result["price"], "100")

    def test_official_paths_and_close(self):
        client = GateClient("k", "s", env="testnet")
        calls = []

        def fake(method, path, qs="", body=None):
            calls.append((method, path, qs, body))
            if path.endswith("/accounts"):
                return {"position_mode": "dual", "in_dual_mode": True, "available": "10", "total": "10"}
            return {"id": 1, "is_close": True}

        client.rest_signed_request = fake
        client._position_mode_cache = (None, 0.0)
        self.assertEqual(client.get_position_mode(), "dual")
        self.assertTrue(any(c[1].endswith("/accounts") for c in calls))

        client.set_leverage("BTC_USDT", 5)
        lev = [c for c in calls if "leverage" in c[1]][-1]
        self.assertIn("dual_comp", lev[1])
        self.assertEqual(lev[2], "leverage=5")
        self.assertIsNone(lev[3])

        single = GateClient("k", "s", env="live")
        single.is_dual_position_mode = lambda: False
        placed = {}
        single.place_order = lambda body: placed.update(body) or {"id": 1, **body}
        single.close_position("BTC_USDT", size=0)
        self.assertEqual(placed.get("size"), 0)
        self.assertTrue(placed.get("close"))
        self.assertTrue(placed.get("reduce_only"))

    def test_trail_business_code_fails(self):
        client = GateClient("k", "s", env="testnet")
        client.rest_signed_request = lambda *a, **k: {"code": -1, "message": "InvalidRequest"}
        with self.assertRaises(GateApiError):
            client.place_trailing_order({"contract": "BTC_USDT"})

    def test_market_slippage_fallback_limit(self):
        client = GateClient("k", "s", env="testnet")
        attempts = []

        def fake(method, path, qs="", body=None):
            attempts.append(body)
            if body.get("price") == "0":
                raise GateApiError("slip", status=400, label="MARKET_PRICE_TOO_DEVIATED")
            return {"id": 9, "price": body.get("price")}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        r = client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(r["price"], "100")

    def test_official_paths_and_close(self):
        client = GateClient("k", "s", env="testnet")
        calls = []

        def fake(method, path, qs="", body=None):
            calls.append((method, path, qs, body))
            if path.endswith("/accounts"):
                return {"position_mode": "dual", "in_dual_mode": True, "available": "10", "total": "10"}
            return {"id": 1}

        client.rest_signed_request = fake
        client._position_mode_cache = (None, 0.0)
        self.assertEqual(client.get_position_mode(), "dual")
        self.assertTrue(any(p.endswith("/accounts") for _, p, _, _ in calls))
        client.set_leverage("BTC_USDT", 5)
        lev = [c for c in calls if c[1].endswith("/leverage")][-1]
        self.assertIn("dual_comp", lev[1])
        self.assertEqual(lev[2], "leverage=5")
        self.assertIsNone(lev[3])

        single = GateClient("k", "s", env="live")
        single.is_dual_position_mode = lambda: False
        placed = {}
        single.place_order = lambda body: placed.update(body) or {"id": 1, **body}
        single.close_position("BTC_USDT", size=0)
        self.assertEqual(placed.get("size"), 0)
        self.assertTrue(placed.get("close"))
        self.assertTrue(placed.get("reduce_only"))

    def test_trail_rejects_business_code(self):
        client = GateClient("k", "s", env="testnet")
        client.rest_signed_request = lambda *a, **k: {"code": -1, "message": "InvalidRequest"}
        with self.assertRaises(GateApiError):
            client.place_trailing_order({"contract": "BTC_USDT"})

    def test_market_slippage_fallback(self):
        client = GateClient("k", "s", env="testnet")

        def fake(method, path, qs="", body=None):
            if body and str(body.get("price")) == "0":
                raise GateApiError("slip", status=400, label="MARKET_PRICE_TOO_DEVIATED")
            return {"id": 9, "price": (body or {}).get("price")}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        r = client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(r["price"], "100")

    def test_official_accounts_and_leverage(self):
        client = GateClient("k", "s", env="testnet")
        calls = []

        def fake(method, path, qs="", body=None):
            calls.append((method, path, qs, body))
            if path.endswith("/accounts"):
                return {"position_mode": "dual", "in_dual_mode": True, "available": "10", "total": "10"}
            return {"id": 1}

        client.rest_signed_request = fake
        client._position_mode_cache = (None, 0.0)
        self.assertEqual(client.get_position_mode(), "dual")
        self.assertTrue(any(p.endswith("/accounts") for _, p, _, _ in calls))
        client.set_leverage("BTC_USDT", 5)
        lev = [c for c in calls if c[1].endswith("/leverage")][-1]
        self.assertIn("dual_comp", lev[1])
        self.assertEqual(lev[2], "leverage=5")
        self.assertIsNone(lev[3])

    def test_official_single_close(self):
        client = GateClient("k", "s", env="live")
        client.is_dual_position_mode = lambda: False
        placed = {}
        client.place_order = lambda body: placed.update(body) or {"id": 1, **body}
        client.close_position("BTC_USDT", size=0)
        self.assertEqual(placed.get("size"), 0)
        self.assertTrue(placed.get("close"))
        self.assertTrue(placed.get("reduce_only"))

    def test_trail_business_code(self):
        client = GateClient("k", "s", env="testnet")
        client.rest_signed_request = lambda *a, **k: {"code": -1, "message": "InvalidRequest"}
        with self.assertRaises(GateApiError):
            client.place_trailing_order({"contract": "BTC_USDT"})

    def test_market_slippage_fallback(self):
        client = GateClient("k", "s", env="testnet")

        def fake(method, path, qs="", body=None):
            if body and str(body.get("price")) == "0":
                raise GateApiError("slip", status=400, label="MARKET_PRICE_TOO_DEVIATED")
            return {"id": 9, "price": (body or {}).get("price")}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        r = client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(r["price"], "100")

    def test_official_paths_and_close(self):
        client = GateClient("k", "s", env="testnet")
        calls = []

        def fake(method, path, qs="", body=None):
            calls.append((method, path, qs, body))
            if path.endswith("/accounts"):
                return {"position_mode": "dual", "in_dual_mode": True, "available": "10", "total": "10"}
            return {"id": 1, "is_close": True}

        client.rest_signed_request = fake
        client._position_mode_cache = (None, 0.0)
        self.assertEqual(client.get_position_mode(), "dual")
        self.assertTrue(any(c[1].endswith("/accounts") for c in calls))
        client.set_leverage("BTC_USDT", 5)
        lev = [c for c in calls if c[1].endswith("/leverage")][-1]
        self.assertIn("dual_comp", lev[1])
        self.assertEqual(lev[2], "leverage=5")
        self.assertIsNone(lev[3])

        single = GateClient("k", "s", env="live")
        single.is_dual_position_mode = lambda: False
        placed = {}
        single.place_order = lambda body: placed.update(body) or {"id": 1, **body}
        single.close_position("BTC_USDT", size=0)
        self.assertEqual(placed.get("size"), 0)
        self.assertTrue(placed.get("close"))
        self.assertTrue(placed.get("reduce_only"))

    def test_trail_business_code_fails(self):
        client = GateClient("k", "s", env="testnet")
        client.rest_signed_request = lambda *a, **k: {"code": -1, "message": "InvalidRequest"}
        with self.assertRaises(GateApiError):
            client.place_trailing_order({"contract": "BTC_USDT"})

    def test_market_slippage_fallback_limit(self):
        client = GateClient("k", "s", env="testnet")
        attempts = []

        def fake(method, path, qs="", body=None):
            attempts.append(body)
            if str(body.get("price")) == "0":
                raise GateApiError("slip", status=400, label="MARKET_PRICE_TOO_DEVIATED")
            return {"id": 9, "price": body.get("price")}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        r = client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(r["price"], "100")

    def test_official_paths_and_close_and_trail(self):
        client = GateClient("k", "s", env="testnet")
        calls = []

        def fake(method, path, qs="", body=None):
            calls.append((method, path, qs, body))
            if path.endswith("/accounts"):
                return {"position_mode": "dual", "in_dual_mode": True, "available": "10", "total": "10"}
            return {"id": 1}

        client.rest_signed_request = fake
        client._position_mode_cache = (None, 0.0)
        self.assertEqual(client.get_position_mode(), "dual")
        self.assertTrue(any(p.endswith("/accounts") for _, p, _, _ in calls))
        client.set_leverage("BTC_USDT", 5)
        lev = [c for c in calls if c[1].endswith("/leverage")][-1]
        self.assertIn("dual_comp", lev[1])
        self.assertEqual(lev[2], "leverage=5")
        self.assertIsNone(lev[3])

        single = GateClient("k", "s", env="live")
        single.is_dual_position_mode = lambda: False
        placed = {}
        single.place_order = lambda body: placed.update(body) or {"id": 1, **body}
        single.close_position("BTC_USDT", size=0)
        self.assertEqual(placed.get("size"), 0)
        self.assertTrue(placed.get("close"))
        self.assertTrue(placed.get("reduce_only"))

        client.rest_signed_request = lambda *a, **k: {"code": -1, "message": "InvalidRequest"}
        with self.assertRaises(GateApiError):
            client.place_trailing_order({"contract": "BTC_USDT"})

        client2 = GateClient("k", "s", env="testnet")
        attempts = []

        def fake2(method, path, qs="", body=None):
            attempts.append(body)
            if str(body.get("price")) == "0":
                raise GateApiError("slip", status=400, label="MARKET_PRICE_TOO_DEVIATED")
            return {"id": 9, "price": body.get("price")}

        client2.rest_signed_request = fake2
        client2.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        r = client2.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(r["price"], "100")

    def test_load_credentials(self):
        keys = ("GATE_API_KEY", "GATE_API_SECRET", "GATE_TESTNET_API_KEY", "GATE_TESTNET_API_SECRET")
        old = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        os.environ["GATE_API_KEY"] = "k"
        os.environ["GATE_API_SECRET"] = "s"
        try:
            self.assertEqual(load_credentials("live"), ("k", "s"))
            with self.assertRaises(GateApiError):
                load_credentials("testnet")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


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
            f = paths.bot_inbox("alpha") / "hold.json"
            f.write_text(json.dumps({"action": "hold"}), encoding="utf-8")
            ok = process_file(f, bot, paths, executor=Executor(FakeClient()))
            self.assertTrue(ok)
            self.assertTrue((paths.bot_done("alpha") / "hold.json").exists())

    def test_staged_name_normalized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = ProjectPaths(root)
            paths.ensure()
            bot = BotConfig(bot_id="alpha", env="testnet")
            f = paths.bot_inbox("alpha") / ".sig.json.staged.json"
            f.write_text(json.dumps({"action": "hold"}), encoding="utf-8")
            ok = process_file(f, bot, paths, executor=Executor(FakeClient()))
            self.assertTrue(ok)
            self.assertTrue((paths.bot_done("alpha") / "sig.json").exists())


if __name__ == "__main__":
    unittest.main()
