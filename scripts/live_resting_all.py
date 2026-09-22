# -*- coding: utf-8 -*-
"""Live: test remaining features WITHOUT market fills (far prices / rejects / config)."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.gate_client import GateApiError, GateClient
from gate_bot.executor import Executor
from gate_bot.schema import parse_signal

KEY = os.environ.get("GATE_API_KEY", "")
SEC = os.environ.get("GATE_API_SECRET", "")

c = GateClient(KEY, SEC, env="live")
ex = Executor(c, symbols_whitelist=["BTC_USDT", "ETH_USDT"], max_notional_usd=50)
last = c.get_last_price("BTC_USDT")
print("BANNER", c.banner())
print("MODE", c.get_position_mode(), "avail", c.get_available_usdt(), "last", last)

results = []


def rec(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", str(detail)[:90]))
    print(("PASS" if ok else "FAIL"), name, str(detail)[:90])


def run(name, payload, expect_ok=True):
    try:
        s = ex.execute_signal(parse_signal(payload)).results[0]
        rec(name, s.ok == expect_ok, s.error or f"{s.action}->{(s.detail or {}).get('executed_as')}")
        return s
    except Exception as e:
        rec(name, expect_ok is False, repr(e)[:90])
        return None


print("\n=== 1 no-op ===")
run("hold", {"action": "hold"})
run("watch", {"action": "watch"})
run("skip", {"action": "skip"})
run("empty_action", {"symbol": "BTC_USDT"})
run("empty_orders", {"orders": []})

print("\n=== 2 guards ===")
run("whitelist_reject", {"action": "open_long", "symbol": "DOGE_USDT", "size": 1, "type": "limit", "price": 1}, expect_ok=False)
run("notional_reject", {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 10000, "type": "limit", "price": round(last * 0.8, 1)}, expect_ok=False)
run("tiny_size_reject", {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 0.01, "type": "limit", "price": round(last * 0.8, 1)}, expect_ok=False)
run("limit_needs_price", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit"}, expect_ok=False)

print("\n=== 3 config: leverage / margin_mode ===")
try:
    r = c.set_leverage("BTC_USDT", 5)
    if isinstance(r, list):
        lev = (r[0] or {}).get("leverage") if r else None
    else:
        lev = (r or {}).get("leverage")
    rec("set_leverage", lev in ("5", 5, "5.0"), lev)
except GateApiError as e:
    rec("set_leverage", False, str(e))
try:
    r = c.set_margin_mode("BTC_USDT", "isolated")
    rec("margin_isolated", True, "ok")
    r = c.set_margin_mode("BTC_USDT", "cross")
    rec("margin_cross", True, "ok")
except GateApiError as e:
    rec("margin_mode", False, str(e))

print("\n=== 4 far limit types ===")
run("limit_long_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit", "price": round(last * 0.80, 1), "label": "live2-ll"})
run("limit_short_far", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "limit", "price": round(last * 1.20, 1), "label": "live2-ls"})
run("postonly_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "post_only", "price": round(last * 0.82, 1), "label": "live2-po"})
run("ioc_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "ioc", "price": round(last * 0.80, 1), "label": "live2-ioc"})
run("fok_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "fok", "price": round(last * 0.80, 1), "label": "live2-fok"}, expect_ok=False)

print("\n=== 5 add/reduce logs (far limits) ===")
run("add_long_far", {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "limit", "price": round(last * 0.81, 1), "label": "live2-al"})
run("add_short_far", {"action": "add_short", "symbol": "BTC_USDT", "size": 1, "type": "limit", "price": round(last * 1.19, 1), "label": "live2-as"})
run("reduce_no_pos", {"action": "reduce_long", "symbol": "BTC_USDT", "size": 1}, expect_ok=False)
if c.is_dual_position_mode():
    run("reduce_no_side", {"action": "reduce", "symbol": "BTC_USDT", "size": 1}, expect_ok=False)
run("flatten_no_pos", {"action": "flatten", "symbol": "BTC_USDT"}, expect_ok=False)

print("\n=== 6 stop_entry aliases far ===")
run("stop_entry_long", {"action": "stop_entry_long", "symbol": "BTC_USDT", "size": 1, "trigger_price": round(last * 1.18, 1), "label": "live2-seL"})
run("buy_stop", {"action": "buy_stop", "symbol": "BTC_USDT", "size": 1, "trigger_price": round(last * 1.16, 1), "label": "live2-bs"})
run("stop_entry_short", {"action": "stop_entry_short", "symbol": "BTC_USDT", "size": 1, "trigger_price": round(last * 0.84, 1), "label": "live2-seS"})
run("sell_stop", {"action": "sell_stop", "symbol": "BTC_USDT", "size": 1, "trigger_price": round(last * 0.82, 1), "label": "live2-ss"})

print("\n=== 7 grid / multi far ===")
run("grid", {"action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
             "levels": [{"price": round(last * 0.83, 1), "size": 1}, {"price": round(last * 0.84, 1), "size": 1}],
             "label": "live2-g"})
run("orders_multi", {"orders": [
    {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "limit", "price": round(last * 0.86, 1), "label": "live2-m1"},
    {"action": "add_short", "symbol": "BTC_USDT", "size": 1, "type": "limit", "price": round(last * 1.14, 1), "label": "live2-m2"},
]})

print("\n=== 8 trail ===")
run("trail", {"action": "trail", "symbol": "BTC_USDT", "amount": -1, "price_offset": "0.5", "activation_price": "0", "label": "live2-tr"})

print("\n=== 9 trigger_expiration on stop_entry ===")
run("se_exp", {"action": "stop_entry_long", "symbol": "BTC_USDT", "size": 1,
               "trigger_price": round(last * 1.22, 1), "trigger_expiration": 86400, "label": "live2-exp"})

print("\n=== snapshot ===")
print("ORDERS", json.dumps([{k: o.get(k) for k in ("id", "size", "price", "tif", "text", "status")}
                            for o in c.list_orders("BTC_USDT")], ensure_ascii=False, indent=2))
print("PRICE_ORDERS")
for p in c.list_price_orders("BTC_USDT"):
    tr, ini = p.get("trigger") or {}, p.get("initial") or {}
    print(json.dumps({"id": p.get("id"), "text": ini.get("text"), "trig": tr.get("price"),
                      "rule": tr.get("rule"), "size": ini.get("size"), "reduce": ini.get("is_reduce_only"),
                      "exp": tr.get("expiration")}, ensure_ascii=False))
print("POS", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0)])
print("TRAIL?", "see log")

print("\n===== SUMMARY =====")
print("PASS", sum(1 for r in results if r[1] == "PASS"), "FAIL", sum(1 for r in results if r[1] == "FAIL"))
for n, st, d in results:
    if st == "FAIL":
        print(" ", n, d)
