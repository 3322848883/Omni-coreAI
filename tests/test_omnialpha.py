import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import (
    ContractMeta,
    GateApiError,
    GateClient,
    load_credentials,
    resolve_symbol,
)  # noqa: E402
from omnialpha.schema import (  # noqa: E402
    SchemaError,
    expand_signal,
    infer_trigger_rules,
    parse_signal,
)
from omnialpha.sizing import default_trigger_limit_price, pct_to_size_usd, usd_to_contracts  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.watcher import ProjectPaths, process_file  # noqa: E402
from omnialpha.config import BotConfig  # noqa: E402


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

    def test_stop_entry_rules_follow_position_side(self):
        """突破单的 TP/SL rule 只看持仓方向，与入场是突破还是限价无关。

        回归背景（2026-10-07 实盘）：`infer_trigger_rules` 不认识
        `stop_entry_*`，落到末尾 `else` 把做空的止损推成 rule=2（跌破触发）——
        而做空止损在**上方**，合法 rule 是 1。
        """
        self.assertEqual(infer_trigger_rules("stop_entry_long", True), 1)
        self.assertEqual(infer_trigger_rules("stop_entry_long", False), 2)
        self.assertEqual(infer_trigger_rules("stop_entry_short", True), 2)
        self.assertEqual(infer_trigger_rules("stop_entry_short", False), 1)

    def test_stop_entry_parses_sl_rule(self):
        """`stop_entry_*` 必须像 `open_*` 一样把 `trigger_rule_sl` 落定，不留 None。"""
        sig = parse_signal({
            "action": "stop_entry_short", "symbol": "BTC_USDT",
            "size_usd": 100, "trigger_price": 83460, "tp": 83190, "sl": 83730,
        })
        it = sig.intents[0]
        self.assertEqual(it.trigger_rule_sl, 1, "做空止损在上方 → rule=1 涨破触发")
        self.assertEqual(it.trigger_rule_tp, 2, "做空止盈在下方 → rule=2 跌破触发")

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

    def test_replace_place_before_cancel_owned(self):
        """New protection first; then withdraw old owned orders only."""
        client = FakeClient()
        ex = Executor(client, symbols_whitelist=["BTC_USDT", "ETH_USDT"])
        # pre-existing owned SL (same strategy prefix)
        client.place_price_order({
            "contract": "BTC_USDT",
            "initial": {"text": "t-demo-legacy-sl"},
            "trigger": {"price": "1"},
        })
        rep = ex.execute_signal(parse_signal({
            "replace": True,
            "action": "open_long",
            "symbol": "BTC_USDT",
            "size": 1,
            "label": "demo",
            "sl": 90,
        }))
        self.assertTrue(rep.ok)
        kinds = [r.action for r in rep.results]
        # open (with new sl) before replace_cancel
        self.assertIn("open_long", kinds)
        self.assertIn("replace_cancel", kinds)
        self.assertLess(kinds.index("open_long"), kinds.index("replace_cancel"))
        # only owned stale price id cancelled — not wipe-all
        self.assertTrue(any(isinstance(c, tuple) and c[0] == "price" for c in client.cancelled))

    def test_replace_all_scope_wipes_whitelist(self):
        client = FakeClient()
        ex = Executor(client, symbols_whitelist=["BTC_USDT", "ETH_USDT"], order_scope="all")
        rep = ex.execute_signal(parse_signal({
            "replace": "all",
            "orders": [{"action": "hold"}],
        }))
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
        # 被门挡住 = **良性跳过**（不再算整轮失败，否则白烧周期 + 污染失败归档）
        # 但必须留可观测标记：否则「被门挡住」与「AI 本来就想 hold」在报告里分不出来
        self.assertTrue(rep.ok, "被门挡住不应算整轮失败")
        self.assertIn("POSITION_EXISTS", rep.results[0].detail.get("gate_skipped", ""))
        # same-side add allowed
        rep2 = ex.execute_signal(parse_signal({"action": "add_long", "symbol": "BTC_USDT", "size": 1}))
        self.assertTrue(rep2.ok)
        # reduce allowed
        rep3 = ex.execute_signal(parse_signal({"action": "reduce_long", "symbol": "BTC_USDT", "size": 1}))
        self.assertTrue(rep3.ok)

    def test_gate_skip_does_not_abort_rest_of_signal(self):
        """被门挡住的 intent 不再 `break` —— 同一信号里后面的合法 intent 仍要执行。

        原来 `_entry_gate` 拒绝后直接 `break`，于是 `[stop_entry_long, modify_tp_sl]`
        里第一个被拒，合法的「改保护」也一起丢了（模拟盘与实盘都踩过）。
        """
        client = FakeClient()
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 2, "mode": "single"}]
        client.price_orders = [
            {"initial": {"text": "t-brk-sl", "contract": "BTC_USDT", "size": -2},
             "trigger": {"price": "84230", "rule": 2}},
        ]
        ex = Executor(client, position_policy="strict", symbols_whitelist=["BTC_USDT"],
                      order_scope="own", label_prefix="brk")
        rep = ex.execute_signal(parse_signal({"orders": [
            {"action": "open_long", "symbol": "BTC_USDT", "size": 1},
            {"action": "modify_tp_sl", "symbol": "BTC_USDT", "sl": 84000},
        ]}))
        self.assertEqual(len(rep.results), 2, "后面的 intent 不应被 break 掉")
        # 执行顺序按优先级排（改保护 4 → 开仓 5），所以**按动作查**而不是按索引。
        # 顺序本身由 `test_execution_priority.py` 覆盖。
        by_action = {r.action: r for r in rep.results}
        self.assertIn("gate_skipped", by_action["open_long"].detail)
        self.assertIn("modify_tp_sl", by_action)

    def test_open_long_with_position_maps_to_add(self):
        """有同侧持仓时 open_long 映射成 add_long（带暴露上限）→ 真正下单，而不是被拒。"""
        client = FakeClient()
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 2, "mode": "single"}]
        client.get_account = lambda: {"total": 10000, "available": 10000}
        ex = Executor(client, position_policy="manage_only", symbols_whitelist=["BTC_USDT"],
                      order_scope="own", label_prefix="brk")
        rep = ex.execute_signal(parse_signal(
            {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "sl": 1}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertNotIn("gate_skipped", rep.results[0].detail, "应被映射而不是跳过")

    def test_position_policy_free_allows_entry(self):
        client = FakeClient()
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 2, "mode": "single"}]
        ex = Executor(client, position_policy="free")
        rep = ex.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size": 1, "sl": 1}))
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
        # every grid level carries protection (require_sl)
        self.assertEqual(intents[0].tp, 72000)
        self.assertEqual(intents[0].sl, 68000)
        self.assertEqual(intents[1].tp, 72000)
        self.assertEqual(intents[1].sl, 68000)

    def test_reject_action_plus_orders(self):
        with self.assertRaises(SchemaError):
            parse_signal({"action": "hold", "orders": [{"action": "hold"}]})

    def test_market_tp_sl_default_and_allowed(self):
        sig = parse_signal({
            "action": "open_long",
            "symbol": "BTC_USDT",
            "size_usd": 10,
            "tp": 2,
            "sl": 1,
        })
        self.assertEqual(sig.intents[0].tp_type, "market")
        self.assertEqual(sig.intents[0].sl_type, "market")
        sig2 = parse_signal({
            "action": "open_long",
            "symbol": "BTC_USDT",
            "size_usd": 10,
            "tp": 2,
            "tp_type": "market",
            "sl": 1,
            "sl_type": "limit",
        })
        self.assertEqual(sig2.intents[0].tp_type, "market")
        self.assertEqual(sig2.intents[0].sl_type, "limit")

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

    def test_too_small_floors_to_one_contract(self):
        """不足 1 张 → 抬到 1 张，不抛错。

        抛错会让整轮计划作废并回滚已挂的腿（实测发生过：模型按
        「风险预算 ÷ 止损距离」反推出 16.15，比 1 张的最小名义 25.76 还小）。
        """
        self.assertEqual(usd_to_contracts(1, 50000, self.meta()), 1)

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
        self.positions = []

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
        out = []
        for i, o in enumerate(self.orders):
            if isinstance(o, dict) and o.get("text"):
                row = {"id": i + 1, "text": o.get("text"), "contract": o.get("contract")}
                # 剩余量 / 只减仓标记要透传 —— `_has_pending_entry` 靠它们判「还有没有
                # 未成交的入场单」，而 `left` 是**带符号**的（卖单为负）。
                for k in ("left", "size", "is_reduce_only"):
                    if k in o:
                        row[k] = o[k]
                out.append(row)
        return out

    def list_price_orders(self, contract=None):
        out = []
        for i, p in enumerate(self.price_orders):
            text = (p.get("initial") or {}).get("text") or p.get("text") or ""
            if text:
                ro = (p.get("initial") or {}).get("reduce_only") or p.get("reduce_only") or 0
                sz = (p.get("initial") or {}).get("size") or p.get("size") or 0
                out.append({
                    "id": 100 + i + 1,
                    "initial": {"text": text, "reduce_only": ro, "size": sz},
                    "text": text,
                    "reduce_only": ro,
                    "size": sz,
                    "trigger": p.get("trigger") or {},
                    "status": p.get("status") or "untriggered",
                })
        return out

    def cancel_price_order(self, order_id):
        self.cancelled.append(("price", order_id))
        return {"cancelled": order_id}

    def cancel_order(self, order_id):
        self.cancelled.append(("order", order_id))
        return {"cancelled": order_id}

    def cancel_all_orders(self, contract):
        self.cancelled.append(contract)
        return {"cancelled": contract}

    def cancel_all_price_orders(self, contract=None):
        return {"cancelled": contract or "ALL"}

    def get_positions(self):
        return list(self.positions)

    def get_available_usdt(self):
        return 1000.0

    def place_trailing_order(self, body):
        self.price_orders.append(body)
        return {"id": 200 + len(self.price_orders), **body}

    def stop_trailing_orders(self, contract=None):
        self.cancelled.append(("trail", contract))
        return {"cancelled": contract or "ALL"}


class TestModifyTpSl(unittest.TestCase):
    def _client_with_pos(self):
        client = FakeClient()
        client.positions = [{"contract": "BTC_USDT", "size": 87, "mode": "dual_long"}]
        # pre-existing owned TP/SL
        client.price_orders = [
            {
                "initial": {"text": "t-brk-sl", "contract": "BTC_USDT", "size": -87},
                "trigger": {"price": "84230", "rule": 2},
            },
            {
                "initial": {"text": "t-brk-tp", "contract": "BTC_USDT", "size": -87},
                "trigger": {"price": "84840", "rule": 1},
            },
        ]
        return client

    def test_hold_with_tpsl_modifies(self):
        client = self._client_with_pos()
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own", label_prefix="brk")
        rep = ex.execute_signal(
            parse_signal({"action": "hold", "symbol": "BTC_USDT", "tp": 85150, "sl": 84400})
        )
        self.assertTrue(rep.ok, rep.to_dict())
        step = rep.results[0]
        self.assertEqual(step.action, "modify_tp_sl")
        # placed new tp+sl
        self.assertEqual(len(client.price_orders), 4)
        kinds = {(p["initial"]["text"].rsplit("-", 1)[-1]) for p in client.price_orders}
        self.assertIn("tp", kinds)
        self.assertIn("sl", kinds)
        # cancelled old owned tp/sl
        self.assertEqual(len(client.cancelled), 2)

    def test_modify_sl_only_keeps_tp(self):
        client = self._client_with_pos()
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own", label_prefix="brk")
        rep = ex.execute_signal(
            parse_signal({"action": "modify_tp_sl", "symbol": "BTC_USDT", "sl": 84400})
        )
        self.assertTrue(rep.ok, rep.to_dict())
        # only one new price order (sl)
        self.assertEqual(len(client.price_orders), 3)
        self.assertEqual(client.price_orders[-1]["initial"]["text"], "t-brk-signal-sl")
        # cancelled only old sl, kept old tp
        self.assertEqual(len(client.cancelled), 1)

    def test_modify_without_position_is_benign_noop(self):
        """无持仓时的 modify_tp_sl 是**良性 no-op**（2026-10-02 改）。

        此前它返回 ok=False、整轮失败 —— 白烧一个周期并污染失败归档。实测两种成因：
        模拟盘「同轮先平仓再改保护」（103 笔）与实盘「AI 看到随未成交入场单预挂的保护单
        以为有持仓」（2026-10-02 07:59）。与 close_position() 对已平账本按 no-op success 一致。
        """
        client = FakeClient()  # flat
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        rep = ex.execute_signal(
            parse_signal({"action": "modify_tp_sl", "symbol": "BTC_USDT", "tp": 85000})
        )
        self.assertTrue(rep.ok, "无持仓不应算失败")
        self.assertIn("noop", rep.results[0].detail)

    def test_modify_requires_level(self):
        client = self._client_with_pos()
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        with self.assertRaises(Exception):
            parse_signal({"action": "modify_tp_sl", "symbol": "BTC_USDT"})

    def test_hold_without_tpsl_still_noop(self):
        client = self._client_with_pos()
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        rep = ex.execute_signal(parse_signal({"action": "hold", "symbol": "BTC_USDT"}))
        self.assertTrue(rep.ok)
        self.assertEqual(rep.results[0].action, "hold")
        self.assertEqual(len(client.price_orders), 2)
        self.assertEqual(client.cancelled, [])

    def test_modify_dual_ambiguous_side(self):
        client = self._client_with_pos()
        client.positions = [
            {"contract": "BTC_USDT", "size": 87, "mode": "dual_long"},
            {"contract": "BTC_USDT", "size": -10, "mode": "dual_short"},
        ]
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        rep = ex.execute_signal(
            parse_signal({"action": "modify_tp_sl", "symbol": "BTC_USDT", "sl": 84400})
        )
        self.assertFalse(rep.ok)
        self.assertIn("AMBIGUOUS_SIDE", rep.results[0].error or "")
        # with side it works
        rep2 = ex.execute_signal(
            parse_signal(
                {"action": "modify_tp_sl", "symbol": "BTC_USDT", "side": "long", "sl": 84400}
            )
        )
        self.assertTrue(rep2.ok, rep2.to_dict())


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

    def test_open_dual_tp_split(self):
        """tp+tp2 双止盈：两腿减仓 + 全仓 SL。"""
        client = FakeClient()
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], max_notional_usd=500)
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
            "tp": 52000, "tp2": 55000, "tp1_share": 0.5, "sl": 48000,
        }))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(len(client.price_orders), 3)
        sizes = sorted(abs(p["initial"]["size"]) for p in client.price_orders)
        self.assertEqual(sizes, [10, 10, 20])
        for p in client.price_orders:
            self.assertTrue(p["initial"].get("reduce_only"))

    def test_notional_guard(self):
        client = FakeClient()
        ex = Executor(client, max_notional_usd=50)
        report = ex.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size_usd": 100}))
        self.assertFalse(report.ok)
        self.assertEqual(len(client.orders), 0)

    def test_orphan_cleanup_safety(self):
        """孤儿保护单回收：只撤 flat+reduce_only+本bot的tp/sl（**无待成交入场单**时）。"""
        client = FakeClient()
        client.price_orders = [
            {"initial": {"text": "t-brk-sl", "reduce_only": 1}, "status": "untriggered", "id": "1"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1}, "status": "untriggered", "id": "2"},
            {"initial": {"text": "t-other-tp", "reduce_only": 1}, "status": "untriggered", "id": "4"},
        ]
        client.get_positions = lambda: []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        cancelled = ex._cleanup_orphan_protectors("BTC_USDT")
        self.assertEqual(len(cancelled), 2, "本 bot 的 flat+reduce_only 保护单应被撤")
        cancelled_ids = [c[1] for c in client.cancelled if c[0] == "price"]
        self.assertEqual(len(cancelled_ids), 2)
        self.assertNotIn("4", cancelled_ids)  # 别家命名空间不碰

    def test_orphan_cleanup_skipped_when_pending_limit_entry(self):
        """**普通挂单**形态的待成交入场单 → 整体跳过清理。

        入场单放在 `client.orders`（走 `list_orders` 分支），且 `price_orders` 里
        **不放任何入场单** —— 这样 True 只可能来自普通挂单分支，那条分支才真正被验到。

        旧版本把「普通挂单」那个用例也塞进了 `client.price_orders`，于是两个 subTest
        都在测条件单分支，普通分支一次也没执行过 —— 这正是 `b0307ab`（`left` 为负的
        空头入场单识别不到）能躲过测试的原因。**「测试写了」≠「那个分支被测了」。**
        """
        client = FakeClient()
        client.orders = [{
            "text": "t-brk", "contract": "BTC_USDT",
            "size": -39, "left": -39, "is_reduce_only": False,   # 空头：left 为负
        }]
        client.price_orders = [
            {"initial": {"text": "t-brk-sl", "reduce_only": 1}, "status": "untriggered", "id": "1"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1}, "status": "untriggered", "id": "2"},
        ]
        client.get_positions = lambda: []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        self.assertTrue(ex._has_pending_entry("BTC_USDT"),
                        "普通挂单分支必须独立成立（left=-39 为负，即空头入场单）")
        self.assertEqual(ex._cleanup_orphan_protectors("BTC_USDT"), [],
                         "有待成交入场单时不该撤任何保护单")

    def test_orphan_cleanup_skipped_when_pending_stop_entry(self):
        """**条件单**形态（`stop_entry_*` 突破进场，挂 price_orders）→ 整体跳过清理。

        `client.orders` 为空 —— 这样 True 只可能来自条件单分支。
        """
        client = FakeClient()
        client.orders = []
        client.price_orders = [
            {"initial": {"text": "t-brk-sl", "reduce_only": 1}, "status": "untriggered", "id": "1"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1}, "status": "untriggered", "id": "2"},
            {"initial": {"text": "t-brk", "reduce_only": 0, "size": -39}, "status": "untriggered", "id": "3"},
        ]
        client.get_positions = lambda: []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        self.assertTrue(ex._has_pending_entry("BTC_USDT"),
                        "条件单分支必须独立成立")
        self.assertEqual(ex._cleanup_orphan_protectors("BTC_USDT"), [],
                         "有待成交入场单时不该撤任何保护单")

    def test_orphan_cleanup_skipped_when_position(self):
        """有仓且方向匹配时保留保护单。"""
        client = FakeClient()
        client.price_orders = [
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "1"},
        ]
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 10, "mode": "single"}]
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        cancelled = ex._cleanup_orphan_protectors("BTC_USDT")
        self.assertEqual(cancelled, [])
        self.assertEqual(client.cancelled, [])

    def test_multi_tp_kept_when_position_exists(self):
        """多级止盈：TP1/TP2/TP3+SL 对应多单时全部保留。"""
        client = FakeClient()
        # 多单保护：sell = size 负
        client.price_orders = [
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "1"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "2"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "3"},
            {"initial": {"text": "t-brk-sl", "reduce_only": 1, "size": -30}, "status": "untriggered", "id": "4"},
        ]
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 30, "mode": "single"}]
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        cancelled = ex._cleanup_orphan_protectors("BTC_USDT")
        self.assertEqual(cancelled, [])
        self.assertEqual(client.cancelled, [])

    def test_multi_tp_orphans_after_close(self):
        """多级止盈：平仓后 TP1/2/3+SL 全部变孤儿，应全撤。"""
        client = FakeClient()
        client.price_orders = [
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "1"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "2"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "3"},
            {"initial": {"text": "t-brk-sl", "reduce_only": 1, "size": -30}, "status": "untriggered", "id": "4"},
        ]
        client.get_positions = lambda: []  # 已平
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        cancelled = ex._cleanup_orphan_protectors("BTC_USDT")
        self.assertEqual(len(cancelled), 4)

    def test_multi_tp_partial_only_orphans_wrong_side(self):
        """双仓：多单在、空单已平 → 只撤空单侧保护，保留多单多级TP。"""
        client = FakeClient()
        client.price_orders = [
            # 多单保护（sell，应保留）
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "1"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "2"},
            # 空单残留保护（buy，应撤）
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": 5}, "status": "untriggered", "id": "3"},
            {"initial": {"text": "t-brk-sl", "reduce_only": 1, "size": 5}, "status": "untriggered", "id": "4"},
        ]
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 20, "mode": "dual_long"}]
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        cancelled = ex._cleanup_orphan_protectors("BTC_USDT")
        # 只撤空单侧（id 3,4 → FakeClient id 103,104）
        self.assertEqual(len(cancelled), 2)
        cancelled_ids = [c[1] for c in client.cancelled if c[0] == "price"]
        self.assertNotIn("101", cancelled_ids)
        self.assertNotIn("102", cancelled_ids)
        self.assertIn("103", cancelled_ids)
        self.assertIn("104", cancelled_ids)

    def test_keep_ids_preserves_new_tps(self):
        """keep_ids 保护本轮新挂的多级止盈。"""
        client = FakeClient()
        client.price_orders = [
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "1"},
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -10}, "status": "untriggered", "id": "2"},
        ]
        client.get_positions = lambda: []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        cancelled = ex._cleanup_orphan_protectors("BTC_USDT", keep_ids={"101", "102"})
        self.assertEqual(cancelled, [])

    def test_resync_protectors_after_reduce(self):
        """减仓后保护单张数应同步到剩余持仓。"""
        client = FakeClient()
        client.price_orders = [
            {"initial": {"text": "t-brk-tp", "reduce_only": 1, "size": -30}, "status": "untriggered", "id": "1",
             "trigger": {"rule": 1, "price_type": 0, "price": "84000"}},
            {"initial": {"text": "t-brk-sl", "reduce_only": 1, "size": -30}, "status": "untriggered", "id": "2",
             "trigger": {"rule": 2, "price_type": 0, "price": "83000"}},
        ]
        # 剩余 13 张
        client.get_positions = lambda: [{"contract": "BTC_USDT", "size": 13, "mode": "single"}]
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], label_prefix="brk")
        notes = ex._resync_protectors("BTC_USDT")
        self.assertEqual(len(notes), 2)
        # 两张旧单被撤、两张新单挂出（size=-13）
        self.assertEqual(len(client.cancelled), 2)
        self.assertEqual(len(client.price_orders), 4)
        for p in client.price_orders[2:]:
            self.assertEqual((p.get("initial") or {}).get("size"), -13)

    def test_notional_pct_guard(self):
        """权益比例硬顶：size_usd > equity×50% 须拒。"""
        client = FakeClient()
        client.get_account = lambda: {"total": 1000.0, "available": 1000.0}
        # sl 很近 → 公式期望大，触发的是 pct 硬顶
        ex = Executor(client, max_notional_usd=100000,
                      account_risk={"max_notional_pct": 0.5, "risk_pct": 0.01}, require_sl=False)
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size_usd": 8000, "sl": 49900,
        }))
        self.assertFalse(rep.ok)
        self.assertIn("MAX_NOTIONAL_PCT", rep.results[0].error or "")
        ex2 = Executor(client, max_notional_usd=100000,
                       account_risk={"max_notional_pct": 0.5, "risk_pct": 0.01}, require_sl=False)
        rep2 = ex2.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size_usd": 400, "sl": 49900,
        }))
        self.assertTrue(rep2.ok, rep2.to_dict())

    def test_size_clamped_to_risk_formula(self):
        """仓位验算：名义超过风险公式 1.3 倍时钳制。"""
        client = FakeClient()
        client.get_account = lambda: {"total": 1000.0, "available": 1000.0}
        client.get_last_price = lambda s: 50000.0
        ex = Executor(client, max_notional_usd=1e12,
                      account_risk={"max_notional_pct": 10, "risk_pct": 0.01},
                      require_sl=True)
        # equity=1000, risk=1%, entry=50000, sl=25000 → dist=50%
        # expected = 1000*0.01/0.5 = 20 USDT 名义
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT",
            "size_usd": 100, "price": 50000, "sl": 25000,
        }))
        self.assertTrue(rep.ok, rep.to_dict())
        note = rep.results[0].detail.get("size_align_note") or ""
        self.assertIn("size_clamped", note)
        self.assertLessEqual(float(rep.results[0].detail.get("size_usd") or 0), 30)

    def test_stop_entry_size_clamped_to_risk_formula(self):
        """stop_entry 也必须按风险公式定仓（与 open_long 同源）。

        原先 `_stop_entry` 只过 `_check_notional`、不调 `_align_size_to_risk` ——
        于是「每笔风险 = 权益 × risk_pct」在突破单上整个失效，仓位直接顶到
        max_notional 上限。实测 eth-disc 讨论组 4 轮里 3 轮产出的正是
        `stop_entry_long`，等于风险公式在多数单子上没生效。
        """
        client = FakeClient()
        client.get_account = lambda: {"total": 1000.0, "available": 1000.0}
        client.get_last_price = lambda s: 50000.0
        ex = Executor(client, max_notional_usd=1e12,
                      account_risk={"max_notional_pct": 10, "risk_pct": 0.01},
                      require_sl=True)
        # equity=1000, risk=1%, entry=50000, sl=25000 → dist=50%
        # expected = 1000*0.01/0.5 = 20 USDT 名义
        rep = ex.execute_signal(parse_signal({
            "action": "stop_entry_long", "symbol": "BTC_USDT",
            "size_usd": 100, "price": 50000, "trigger_price": 50100, "sl": 25000,
        }))
        self.assertTrue(rep.ok, rep.to_dict())
        detail = rep.results[0].detail or {}
        self.assertIn("size_clamped", detail.get("size_note") or "",
                      "stop_entry 也必须走风险公式钳位")
        self.assertLessEqual(float(detail.get("size_usd") or 0), 30)

    def test_max_notional_usd_clamps_instead_of_rejecting(self):
        """名义硬顶要**钳制**，不能整笔拒 —— 风险公式期望值超过上限是常态。

        equity 1000 / risk 1% / 止损距离 50% → 期望名义 20；把 max_notional_usd
        设成 10，原先 `_check_notional` 会把整笔 raise 掉，现在应缩到 10 照常开。
        """
        client = FakeClient()
        client.get_account = lambda: {"total": 1000.0, "available": 1000.0}
        client.get_last_price = lambda s: 50000.0
        ex = Executor(client, max_notional_usd=10,
                      account_risk={"max_notional_pct": 10, "risk_pct": 0.01},
                      require_sl=True)
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT",
            "size_usd": 100, "price": 50000, "sl": 25000,
        }))
        self.assertTrue(rep.ok, rep.to_dict())
        detail = rep.results[0].detail or {}
        self.assertLessEqual(float(detail.get("size_usd") or 0), 10.0,
                             "名义硬顶必须钳制，不能整笔拒单")


class TestStopEntrySharesOpenGates(unittest.TestCase):
    """`stop_entry_*` 必须与 `open_*` 走**同一套**闸门。

    回归背景（2026-10-05 架构盘点）：`_stop_entry` 原先只调 `_check_symbol`
    + `_check_notional`，**不调** `_check_account_risk` / `_check_open_sl`
    / `_precheck_exit_triggers` —— 突破单可以绕过 halt / 日亏熔断 / 杠杆上限 /
    总敞口闸门 / SL 必填。而 eth-disc 实测 4 轮里 3 轮产出的正是
    `stop_entry_long`，等于账户级风控在多数单子上根本没生效。
    """

    def _ex(self, client, **kw):
        return Executor(client, symbols_whitelist=["BTC_USDT"], **kw)

    def test_stop_entry_respects_halt(self):
        """halt: true 时 stop_entry 也必须被拒（原先可绕过）。"""
        client = FakeClient()
        client.get_last_price = lambda s: 50000.0
        ex = self._ex(client, require_sl=False, account_risk={"halt": True})
        rep = ex.execute_signal(parse_signal({
            "action": "stop_entry_long", "symbol": "BTC_USDT",
            "size_usd": 100, "trigger_price": 51000,
        }))
        self.assertFalse(rep.ok, "halt 必须拦停突破单")
        self.assertIn("HALT", (rep.results[0].error or "").upper())

    def test_stop_entry_requires_sl(self):
        """require_sl 时 stop_entry 无 sl 必须被拒（原先可绕过）。"""
        client = FakeClient()
        client.get_last_price = lambda s: 50000.0
        ex = self._ex(client, require_sl=True)
        rep = ex.execute_signal(parse_signal({
            "action": "stop_entry_long", "symbol": "BTC_USDT",
            "size_usd": 100, "trigger_price": 51000,
        }))
        self.assertFalse(rep.ok, "突破单也必须带止损")
        self.assertIn("SL_REQUIRED", rep.results[0].error or "")

    def test_stop_entry_checks_exit_trigger_side(self):
        """TP/SL 触发价落在 mark 非法一侧时整笔中止（原先会挂出无保护的入场单）。

        mark 50000 时，多头 SL 必须 < mark：给 51000（高于 mark）应被拦。
        """
        client = FakeClient()
        client.get_last_price = lambda s: 50000.0
        client.get_ticker = lambda s: {"mark_price": 50000.0}
        ex = self._ex(client, require_sl=False)
        rep = ex.execute_signal(parse_signal({
            "action": "stop_entry_long", "symbol": "BTC_USDT",
            "size_usd": 100, "trigger_price": 51000, "sl": 51000,
        }))
        self.assertFalse(rep.ok, "保护单触发价非法时应整笔中止")
        self.assertIn("TRIGGER_PRICE_SIDE", rep.results[0].error or "")

    def test_cancel_all_own_scope_only_touches_label_prefix(self):
        """order_scope=own + explicit bot label must not wipe another bot's book (prelaunch M12)."""
        client = FakeClient()
        client.orders = [
            {"contract": "BTC_USDT", "size": 1, "text": "t-bota-1"},
            {"contract": "BTC_USDT", "size": 1, "text": "t-botb-1"},
            {"contract": "BTC_USDT", "size": 1, "text": "t-botb-2"},
        ]
        client.cancelled = []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own")
        report = ex.execute_signal(
            parse_signal({"action": "cancel_all", "symbol": "BTC_USDT", "label": "bota"})
        )
        self.assertTrue(report.ok)
        self.assertEqual(client.cancelled, [("order", "1")])
        self.assertNotIn(("order", "2"), client.cancelled)
        self.assertNotIn(("order", "3"), client.cancelled)
        self.assertNotIn("BTC_USDT", client.cancelled)

    def test_cancel_all_own_scope_reports_cancel_failure(self):
        """A1: cancel_order failure must not report ok=True (silent residual)."""
        client = FakeClient()
        client.orders = [
            {"contract": "BTC_USDT", "size": 1, "text": "t-bota-1"},
            {"contract": "BTC_USDT", "size": 1, "text": "t-bota-2"},
        ]
        client.cancelled = []

        def cancel_order(oid):
            if str(oid) == "1":
                raise GateApiError("cancel rejected")
            client.cancelled.append(("order", oid))
            return {"cancelled": oid}

        client.cancel_order = cancel_order
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own")
        report = ex.execute_signal(
            parse_signal({"action": "cancel_all", "symbol": "BTC_USDT", "label": "bota"})
        )
        self.assertFalse(report.ok)
        detail = report.results[0].detail or {}
        self.assertEqual(detail.get("cancelled"), ["2"])
        self.assertTrue(detail.get("errors"))

    def test_cancel_price_all_own_scope_reports_cancel_failure(self):
        client = FakeClient()
        client.price_orders = [
            {"initial": {"text": "t-bota-1", "contract": "BTC_USDT"}},
            {"initial": {"text": "t-bota-2", "contract": "BTC_USDT"}},
        ]
        client.cancelled = []

        def cancel_price_order(pid):
            if str(pid) == "101":
                raise GateApiError("price cancel rejected")
            client.cancelled.append(("price", pid))
            return {"cancelled": pid}

        client.cancel_price_order = cancel_price_order
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own")
        report = ex.execute_signal(
            parse_signal({"action": "cancel_price_all", "symbol": "BTC_USDT", "label": "bota"})
        )
        self.assertFalse(report.ok)
        self.assertEqual((report.results[0].detail or {}).get("cancelled"), ["102"])

    def test_cancel_all_default_label_still_wipes(self):
        """Default label 'signal' keeps legacy wipe so cleanup paths work."""
        client = FakeClient()
        client.orders = [
            {"contract": "BTC_USDT", "size": 1, "text": "t-bota-1"},
            {"contract": "BTC_USDT", "size": 1, "text": "t-other"},
        ]
        client.cancelled = []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own")
        report = ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT"}))
        self.assertTrue(report.ok)
        self.assertIn("BTC_USDT", client.cancelled)

    def test_cancel_label_prefix_is_segment_safe(self):
        """label 'bot' must not match t-bota* / t-botb* (prelaunch review)."""
        client = FakeClient()
        client.orders = [
            {"contract": "BTC_USDT", "size": 1, "text": "t-bot"},
            {"contract": "BTC_USDT", "size": 1, "text": "t-bot-sl"},
            {"contract": "BTC_USDT", "size": 1, "text": "t-bota-1"},
            {"contract": "BTC_USDT", "size": 1, "text": "t-botb-1"},
        ]
        client.cancelled = []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own")
        ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT", "label": "bot"}))
        self.assertEqual(sorted(client.cancelled), [("order", "1"), ("order", "2")])
        self.assertNotIn(("order", "3"), client.cancelled)
        self.assertNotIn(("order", "4"), client.cancelled)

    def test_label_prefix_blocks_forged_cross_bot_cancel(self):
        """A3: signal label cannot cancel another bot's book when label_prefix is set."""
        client = FakeClient()
        client.orders = [
            {"contract": "BTC_USDT", "size": 1, "text": "t-alpha-1"},
            {"contract": "BTC_USDT", "size": 1, "text": "t-beta-1"},
        ]
        client.cancelled = []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own", label_prefix="alpha")
        # forged label claiming beta
        report = ex.execute_signal(
            parse_signal({"action": "cancel_all", "symbol": "BTC_USDT", "label": "beta"})
        )
        self.assertTrue(report.ok)
        self.assertEqual(client.cancelled, [("order", "1")])
        self.assertNotIn(("order", "2"), client.cancelled)

    def test_label_prefix_namespaces_new_orders(self):
        client = FakeClient()
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], require_sl=False, label_prefix="alpha")
        ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit",
            "price": 40000, "label": "beta",  # forged / foreign label
        }))
        self.assertEqual(client.orders[0]["text"], "t-alpha-beta")

    def test_cancel_price_all_own_scope_only_touches_label_prefix(self):
        client = FakeClient()
        client.price_orders = [
            {"initial": {"text": "t-bota-1", "contract": "BTC_USDT"}},
            {"initial": {"text": "t-botb-1", "contract": "BTC_USDT"}},
        ]
        client.cancelled = []
        ex = Executor(client, symbols_whitelist=["BTC_USDT"], order_scope="own")
        report = ex.execute_signal(
            parse_signal({"action": "cancel_price_all", "symbol": "BTC_USDT", "label": "bota"})
        )
        self.assertTrue(report.ok)
        self.assertEqual(client.cancelled, [("price", "101")])
        self.assertNotIn(("price", "102"), client.cancelled)

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
        # order_scope=all keeps legacy wipe when cancelling without a unique bot label
        ex = Executor(client, order_scope="all")
        self.assertTrue(ex.execute_signal(parse_signal({"action": "cancel_all"})).ok)
        self.assertEqual(client.cancelled, ["BTC_USDT", "ETH_USDT"])


    def test_require_sl_blocks_naked_open(self):
        client = FakeClient()
        ex = Executor(client, require_sl=True)
        rep = ex.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size": 1}))
        self.assertFalse(rep.ok)
        self.assertIn("SL_REQUIRED", rep.results[0].error or "")

    def test_account_halt_blocks_open(self):
        client = FakeClient()
        ex = Executor(client, require_sl=False, account_risk={"halt": True})
        rep = ex.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size": 1, "sl": 1}))
        self.assertFalse(rep.ok)
        self.assertIn("HALTED", rep.results[0].error or "")

    def test_max_leverage_reject(self):
        client = FakeClient()
        ex = Executor(client, require_sl=False, account_risk={"max_leverage": 5})
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size": 1, "sl": 1, "leverage": 20,
        }))
        self.assertFalse(rep.ok)
        self.assertIn("MAX_LEVERAGE", rep.results[0].error or "")

    def test_idempotent_no_double_on_confirm_flake(self):
        class Flaky(FakeClient):
            def __init__(self):
                super().__init__()
                self.n = 0
                self.placed_texts = []

            def place_order(self, body):
                self.placed_texts.append(body.get("text"))
                return super().place_order(body)

            def get_order(self, oid):
                self.n += 1
                if self.n == 1:
                    raise RuntimeError("timeout")
                return {"id": oid, "status": "open", "left": 1}

            def list_orders(self, contract=None):
                return [
                    {"id": i + 1, "text": o.get("text")}
                    for i, o in enumerate(self.orders)
                    if isinstance(o, dict) and o.get("text")
                ]

        c = Flaky()
        ex = Executor(c, require_sl=True)
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size": 1, "sl": 1, "label": "idem",
        }))
        # entry should succeed without placing twice
        self.assertTrue(rep.ok)
        self.assertEqual(c.placed_texts.count("t-idem"), 1)

    def test_entry_and_exit_hang_simultaneous_same_size(self):
        """Entry + TP/SL hung together at planned size (not wait-for-fill)."""
        class FilledClient(FakeClient):
            def place_order(self, body):
                self.orders.append(body)
                return {"id": len(self.orders), "size": body.get("size"), "left": 0,
                        "finish_as": "filled", "status": "finished", **body}

        c = FilledClient()
        ex = Executor(c)
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size": 5, "sl": 1, "tp": 2, "label": "sim",
        }))
        s = rep.results[0]
        self.assertEqual(s.detail.get("hang_mode"), "simultaneous")
        self.assertEqual(s.detail.get("exit_size"), 5)
        sl = (s.detail.get("sl_orders") or [{}])[0]
        tp = (s.detail.get("tp_orders") or [{}])[0]
        self.assertEqual((sl.get("order") or {}).get("initial", {}).get("size"), -5)
        self.assertEqual((tp.get("order") or {}).get("initial", {}).get("size"), -5)

    def test_open_size_pct(self):
        client = FakeClient()
        ex = Executor(client, max_notional_usd=5000)
        sig = parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size_pct": 0.1, "sl": 48000})
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
            return {"id": 9, "price": body.get("price"), "tif": body.get("tif")}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        result = client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        # market buy must lift the ASK (taker), not rest on the bid
        self.assertEqual(result["price"], "101")
        self.assertEqual(result["tif"], "ioc")

    def test_market_slippage_fallback_sell_hits_bid(self):
        client = GateClient("k", "s", env="testnet")
        captured = {}

        def fake(method, path, qs="", body=None):
            if body and body.get("price") == "0":
                raise GateApiError("slip", status=400, label="MARKET_PRICE_TOO_DEVIATED")
            captured.update(body or {})
            return {"id": 9, **(body or {})}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        client.place_order({"contract": "BTC_USDT", "size": -1, "price": "0", "tif": "ioc"})
        self.assertEqual(captured.get("price"), "100")
        self.assertEqual(captured.get("tif"), "ioc")

    def test_market_fallback_accepts_price_too_deviated_label(self):
        client = GateClient("k", "s", env="testnet")
        captured = {}

        def fake(method, path, qs="", body=None):
            if body and str(body.get("price")) == "0":
                raise GateApiError("slip", status=400, label="PRICE_TOO_DEVIATED")
            captured.update(body or {})
            return {"id": 9, **(body or {})}

        client.rest_signed_request = fake
        client.get_ticker = lambda s: {"last": "100", "mark_price": "100"}
        client.get_contract = lambda s: type("M", (), {"order_price_round": 0.1})()
        # stale wide book: ask 200 — must clamp near last*1.002
        client.public_get = lambda path, qs="": {"bids": [{"p": "50", "s": 1}], "asks": [{"p": "200", "s": 1}]}
        client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(captured.get("tif"), "ioc")
        px = float(captured.get("price"))
        self.assertLess(px, 101.0)
        self.assertGreater(px, 99.0)

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
        self.assertEqual(r["price"], "101")
        self.assertEqual(attempts[-1].get("tif"), "ioc")

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
            return {"id": 9, "price": (body or {}).get("price"), "tif": (body or {}).get("tif")}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        r = client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(r["price"], "101")
        self.assertEqual(r["tif"], "ioc")

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
            return {"id": 9, "price": (body or {}).get("price"), "tif": (body or {}).get("tif")}

        client.rest_signed_request = fake
        client.public_get = lambda path, qs="": {"bids": [{"p": "100", "s": 1}], "asks": [{"p": "101", "s": 1}]}
        r = client.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc"})
        self.assertEqual(r["price"], "101")
        self.assertEqual(r["tif"], "ioc")

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
        self.assertEqual(r["price"], "101")
        self.assertEqual(attempts[-1].get("tif"), "ioc")

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
        self.assertEqual(r["price"], "101")

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
