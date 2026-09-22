# -*- coding: utf-8 -*-
"""Testnet smoke for SINGLE (one-way) position mode."""
import json
from gate_bot.gate_client import GateClient
from gate_bot.executor import Executor
from gate_bot.schema import parse_signal

c = GateClient(
    "7b31134e731b10ea859da2e75440d263",
    "c352fbb46779a1861d2b1facfa33e0d20306785bc1d1bef768e8951a61213b87",
    env="testnet",
)
ex = Executor(c)
# bust mode cache in case it was cached as dual
c._position_mode_cache = (None, 0.0)
last = c.get_last_price("BTC_USDT")
mode = c.get_position_mode()
print("LAST", last, "MODE", mode, "DUAL", c.is_dual_position_mode())


def run(name, payload, expect_ok=True):
    rep = ex.execute_signal(parse_signal(payload))
    s = rep.results[0]
    ok = s.ok == expect_ok
    print("%s %-20s act=%s as=%s err=%s" % (
        "OK " if ok else "NG ", name, s.action,
        (s.detail or {}).get("executed_as"), (s.error or "")[:90]))
    return s


print("--- 0 cleanup ---")
run("cancel_all", {"action": "cancel_all", "symbol": "BTC_USDT"})
run("cancel_price_all", {"action": "cancel_price_all", "symbol": "BTC_USDT"})
run("flatten", {"action": "flatten", "symbol": "BTC_USDT", "label": "sm1-flat0"})
print("pos0", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])

print("--- 1 single: open long then reduce (auto side) ---")
run("open_long", {"action": "open_long", "symbol": "BTC_USDT", "size": 2, "type": "market", "label": "sm1-long"})
run("reduce_no_side", {"action": "reduce", "symbol": "BTC_USDT", "size": 1, "label": "sm1-red"})
print("pos1", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])

print("--- 2 single: add_long again (same direction) ---")
run("add_long", {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm1-addL"})
print("pos2", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])

print("--- 3 single: add_short on long book = FLIP ---")
run("add_short_flip", {"action": "add_short", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm1-flip"})
print("pos3", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])

print("--- 4 single: reduce_long on short book (reduce_only, should not flip) ---")
run("reduce_long_when_short", {"action": "reduce_long", "symbol": "BTC_USDT", "size": 1, "label": "sm1-rl"}, expect_ok=False)
print("pos4", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])

print("--- 5 single: reduce_short closes short ---")
run("reduce_short", {"action": "reduce_short", "symbol": "BTC_USDT", "size": 1, "label": "sm1-rs"})
print("pos5", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])

print("--- 6 single: flatten / close no side ---")
run("open_long2", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm1-l2"})
run("close_no_side", {"action": "close", "symbol": "BTC_USDT", "label": "sm1-c"})
print("pos6", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])

print("--- 7 single: TP/SL + stop_entry ---")
run("open_tp_sl", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                   "tp": round(last * 1.04, 1), "sl": round(last * 0.96, 1),
                   "trigger_price_type": "mark", "label": "sm1-tpsl"})
run("stop_entry_long", {"action": "stop_entry_long", "symbol": "BTC_USDT", "size": 1,
                        "trigger_price": round(last * 1.03, 1), "label": "sm1-se"})
run("reduce_after", {"action": "reduce", "symbol": "BTC_USDT", "size": 1, "label": "sm1-red2"})

print("--- 8 cleanup ---")
run("cancel_all", {"action": "cancel_all", "symbol": "BTC_USDT"})
run("cancel_price_all", {"action": "cancel_price_all", "symbol": "BTC_USDT"})
run("flatten", {"action": "flatten", "symbol": "BTC_USDT", "label": "sm1-flat1"})

print("=== LEFT ORDERS ===", json.dumps([{k: o.get(k) for k in ("id", "size", "price", "text")}
                                         for o in c.list_orders("BTC_USDT")], ensure_ascii=False))
print("=== LEFT PO ===", len(c.list_price_orders("BTC_USDT")))
print("=== POS ===", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])
