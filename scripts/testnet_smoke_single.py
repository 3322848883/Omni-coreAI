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
last = c.get_last_price("BTC_USDT")
print("LAST", last, "MODE", c.get_position_mode(), "DUAL", c.is_dual_position_mode())


def run(name, payload, expect_ok=True):
    rep = ex.execute_signal(parse_signal(payload))
    s = rep.results[0]
    ok = s.ok == expect_ok
    print("%s %-20s act=%s as=%s err=%s" % (
        "OK " if ok else "NG ", name, s.action,
        (s.detail or {}).get("executed_as"), (s.error or "")[:90]))
    return s


def pos():
    return [(p["mode"], p["size"], p.get("entry_price")) for p in c.get_positions() if int(p.get("size") or 0) != 0]


print("--- 1 open long then reduce_auto ---")
run("open_long", {"action": "open_long", "symbol": "BTC_USDT", "size": 2, "type": "market", "label": "sm1-l"})
print("pos", pos())
run("reduce_auto", {"action": "reduce", "symbol": "BTC_USDT", "size": 1, "label": "sm1-r"})
print("pos", pos())

print("--- 2 add_long again ---")
run("add_long", {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm1-al"})
print("pos", pos())

print("--- 3 add_short on long book = FLIP ---")
run("add_short_flip", {"action": "add_short", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm1-flip"})
print("pos", pos())

print("--- 4 reduce_short on short book ---")
run("reduce_short", {"action": "reduce_short", "symbol": "BTC_USDT", "size": 1, "label": "sm1-rs"})
print("pos", pos())

print("--- 5 close no side (auto) ---")
run("open_long2", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm1-l2"})
run("close_auto", {"action": "close", "symbol": "BTC_USDT", "label": "sm1-c"})
print("pos", pos())

print("--- 6 reduce_only must not flip ---")
run("open_long3", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm1-l3"})
run("reduce_short_wrong", {"action": "reduce_short", "symbol": "BTC_USDT", "size": 1, "label": "sm1-rsw"}, expect_ok=False)
print("pos", pos())

print("--- 7 flatten ---")
run("flatten", {"action": "flatten", "symbol": "BTC_USDT", "label": "sm1-f"})
print("pos", pos())
run("cancel_all", {"action": "cancel_all", "symbol": "BTC_USDT"})
run("cancel_price_all", {"action": "cancel_price_all", "symbol": "BTC_USDT"})
print("orders", json.dumps([{k: o.get(k) for k in ("id", "text", "size")} for o in c.list_orders("BTC_USDT")], ensure_ascii=False))
print("po", len(c.list_price_orders("BTC_USDT")))
