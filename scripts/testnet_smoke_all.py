# -*- coding: utf-8 -*-
"""Testnet smoke: all OmniAlpha features including position-mode add/reduce."""
import json
from omnialpha.gate_client import GateClient
from omnialpha.executor import Executor
from omnialpha.schema import parse_signal

c = GateClient(
    "7b31134e731b10ea859da2e75440d263",
    "c352fbb46779a1861d2b1facfa33e0d20306785bc1d1bef768e8951a61213b87",
    env="testnet",
)
ex = Executor(c)
last = c.get_last_price("BTC_USDT")
mode = c.get_position_mode()
print("LAST", last, "MODE", mode, "DUAL", c.is_dual_position_mode())


def run(name, payload, expect_ok=True):
    rep = ex.execute_signal(parse_signal(payload))
    s = rep.results[0]
    ok = (s.ok == expect_ok)
    print("%s %-18s act=%s as=%s err=%s" % (
        "OK " if ok else "NG ", name, s.action, (s.detail or {}).get("executed_as"), (s.error or "")[:80]))
    return s


print("--- 1 hold/watch ---")
run("hold", {"action": "hold"})
run("watch", {"action": "watch"})

print("--- 2 open market ---")
run("open_long_mkt", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm-mkt-long"})
run("open_short_mkt", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm-mkt-short"})

print("--- 3 limit / post_only / ioc / fok ---")
run("open_long_lim", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit",
                      "price": round(last * 0.97, 1), "label": "sm-lim-long"})
run("open_short_lim", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "limit",
                       "price": round(last * 1.03, 1), "label": "sm-lim-short"})
run("post_only", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "post_only",
                  "price": round(last * 0.98, 1), "label": "sm-po"})
run("ioc_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "ioc",
                "price": round(last * 0.90, 1), "label": "sm-ioc"})
run("fok_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "fok",
                "price": round(last * 0.90, 1), "label": "sm-fok"}, expect_ok=False)

print("--- 4 add / reduce (position-mode aware) ---")
run("add_long", {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm-addL"})
run("add_short", {"action": "add_short", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "sm-addS"})
run("reduce_long", {"action": "reduce_long", "symbol": "BTC_USDT", "size": 1, "label": "sm-redL"})
run("reduce_short", {"action": "reduce_short", "symbol": "BTC_USDT", "size": 1, "label": "sm-redS"})
if c.is_dual_position_mode():
    run("reduce_dual_no_side", {"action": "reduce", "symbol": "BTC_USDT", "size": 1}, expect_ok=False)
    run("reduce_dual_side", {"action": "reduce", "symbol": "BTC_USDT", "side": "long", "size": 1, "label": "sm-red"})
else:
    run("reduce_single", {"action": "reduce", "symbol": "BTC_USDT", "size": 1, "label": "sm-red"})

print("--- 5 size_pct / margin_pct ---")
run("size_pct", {"action": "add_long", "symbol": "BTC_USDT", "size_pct": 0.02, "type": "market", "label": "sm-pct"})
run("margin_pct", {"action": "add_long", "symbol": "BTC_USDT", "margin_pct": 0.02, "leverage": 5,
                   "type": "market", "label": "sm-mpct"})

print("--- 6 TP / SL ---")
run("open_tp_sl", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                   "tp": round(last * 1.05, 1), "sl": round(last * 0.95, 1),
                   "trigger_price_type": "mark", "label": "sm-tpsl"})

print("--- 7 stop_entry (breakout) ---")
run("stop_entry_long", {"action": "stop_entry_long", "symbol": "BTC_USDT", "size": 1,
                        "trigger_price": round(last * 1.03, 1), "label": "sm-seL"})
run("buy_stop_alias", {"action": "buy_stop", "symbol": "BTC_USDT", "size": 1,
                       "trigger_price": round(last * 1.04, 1), "label": "sm-bs"})
run("stop_entry_short", {"action": "stop_entry_short", "symbol": "BTC_USDT", "size": 1,
                         "trigger_price": round(last * 0.97, 1), "label": "sm-seS"})

print("--- 8 grid / multi ---")
run("grid_long", {"action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
                  "levels": [{"price": round(last * 0.96, 1), "size": 1},
                             {"price": round(last * 0.95, 1), "size": 1}],
                  "label": "sm-grid"})
run("orders_multi", {"orders": [
    {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "limit",
     "price": round(last * 0.94, 1), "label": "sm-m1"},
    {"action": "add_short", "symbol": "BTC_USDT", "size": 1, "type": "limit",
     "price": round(last * 1.06, 1), "label": "sm-m2"},
]})

print("--- 9 trail ---")
run("trail", {"action": "trail", "symbol": "BTC_USDT", "amount": -1, "price_offset": "0.5",
              "activation_price": "0", "label": "sm-trail"})

print("--- 10 cancel / flatten ---")
run("cancel_all", {"action": "cancel_all", "symbol": "BTC_USDT"})
run("cancel_price_all", {"action": "cancel_price_all", "symbol": "BTC_USDT"})
run("flatten", {"action": "flatten", "symbol": "BTC_USDT", "label": "sm-flat"})

print("=== LEFT ORDERS ===", json.dumps([{k: o.get(k) for k in ("id", "size", "price", "tif", "text")}
                                         for o in c.list_orders("BTC_USDT")], ensure_ascii=False))
print("=== LEFT PO ===", len(c.list_price_orders("BTC_USDT")))
print("=== POS ===", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])
