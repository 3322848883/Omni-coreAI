# -*- coding: utf-8 -*-
"""Exhaustive testnet checks for remaining untested features."""
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.gate_client import GateApiError, GateClient
from gate_bot.config import BotConfig
from gate_bot.executor import Executor
from gate_bot.schema import parse_signal
from gate_bot.watcher import ProjectPaths, process_file, run_bot_once

KEY = "7b31134e731b10ea859da2e75440d263"
SEC = "c352fbb46779a1861d2b1facfa33e0d20306785bc1d1bef768e8951a61213b87"
os.environ.setdefault("GATE_TESTNET_API_KEY", KEY)
os.environ.setdefault("GATE_TESTNET_API_SECRET", SEC)

results = []


def rec(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", str(detail)[:90]))
    print("%s %-28s %s" % ("PASS" if ok else "FAIL", name, str(detail)[:90]))


c = GateClient(KEY, SEC, env="testnet")
ex = Executor(c, symbols_whitelist=["BTC_USDT", "ETH_USDT"], max_notional_usd=500)
last = c.get_last_price("BTC_USDT")
print("MODE", c.get_position_mode(), "DUAL", c.is_dual_position_mode(), "LAST", last)


def run(name, payload, expect_ok=True, executor=None):
    e = executor or ex
    rep = e.execute_signal(parse_signal(payload))
    s = rep.results[0]
    ok = s.ok == expect_ok
    rec(name, ok, s.error or s.action)
    return s


print("\n=== 0 cleanup ===")
for sym, n in (("BTC_USDT", 5), ("ETH_USDT", 5), ("SOL_USDT", 2), ("DOGE_USDT", 2), ("XRP_USDT", 2)):
    try:
        c.place_order({"contract": sym, "size": n, "price": "0", "tif": "ioc", "reduce_only": True, "text": "t-clnS"})
        c.place_order({"contract": sym, "size": -n, "price": "0", "tif": "ioc", "reduce_only": True, "text": "t-clnL"})
    except GateApiError:
        pass
rec("cleanup_flat", all(int(p.get("size") or 0) == 0 for p in c.get_positions()), "flat")

print("\n=== 1 hold/watch/skip/empty ===")
run("hold", {"action": "hold"})
run("watch", {"action": "watch"})
run("skip", {"action": "skip"})
run("empty_action", {"symbol": "BTC_USDT"})
run("empty_orders", {"orders": []})

print("\n=== 2 whitelist / notional / tiny size ===")
run("whitelist_reject", {"action": "open_long", "symbol": "DOGE_USDT", "size": 1, "type": "market"}, expect_ok=False)
run("notional_reject", {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 99999}, expect_ok=False)
run("tiny_size_reject", {"action": "open_long", "symbol": "BTC_USDT", "size_usd": 0.01}, expect_ok=False)

print("\n=== 3 FOK/IOC ===")
run("fok_far_reject", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "fok",
                       "price": round(last * 0.9, 1)}, expect_ok=False)
run("ioc_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "ioc",
                "price": round(last * 0.9, 1)})
# FOK fillable: reduce into bid if position exists after add
run("add_for_fok", {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "t-foksrc"})
ob = c.public_get("/api/v4/futures/usdt/order_book", "contract=BTC_USDT&limit=1")
bid = str(ob["bids"][0]["p"])
try:
    r = c.place_order({"contract": "BTC_USDT", "size": -1, "price": bid, "tif": "fok", "reduce_only": True, "text": "t-fokfill"})
    rec("fok_fill_reduce", r.get("status") == "finished" or int(r.get("left") or 0) == 0, r.get("id"))
except GateApiError as e:
    rec("fok_fill_reduce", False, str(e))

print("\n=== 4 isolated / expiration / stop_entry ===")
run("isolated", {"action": "open_long", "symbol": "ETH_USDT", "size": 1, "type": "market",
                 "margin_mode": "isolated", "label": "t-iso"})
run("tpsl_expiration", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                        "tp": round(last * 1.03, 1), "sl": round(last * 0.97, 1),
                        "trigger_expiration": 3600, "label": "t-exp"})
run("stop_entry_long", {"action": "stop_entry_long", "symbol": "BTC_USDT", "size": 1,
                        "trigger_price": round(last * 1.02, 1), "label": "t-se"})
run("buy_stop_alias", {"action": "buy_stop", "symbol": "BTC_USDT", "size": 1,
                       "trigger_price": round(last * 1.03, 1), "label": "t-bs"})

print("\n=== 5 grid / multi / add-reduce ===")
run("grid", {"action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
             "levels": [{"price": round(last * 0.99, 1), "size": 1}], "label": "t-g"})
run("orders_multi", {"orders": [
    {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "limit", "price": round(last * 0.98, 1)},
    {"action": "add_short", "symbol": "BTC_USDT", "size": 1, "type": "limit", "price": round(last * 1.02, 1)},
]})
run("add_long", {"action": "add_long", "symbol": "BTC_USDT", "size": 1, "type": "market", "label": "t-al"})
run("reduce_long", {"action": "reduce_long", "symbol": "BTC_USDT", "size": 1, "label": "t-rl"})
if c.is_dual_position_mode():
    run("reduce_no_side_dual", {"action": "reduce", "symbol": "BTC_USDT", "size": 1}, expect_ok=False)
else:
    run("reduce_auto_single", {"action": "reduce", "symbol": "BTC_USDT", "size": 1, "label": "t-ra"})

print("\n=== 6 trail ===")
s_trail = run("trail", {"action": "trail", "symbol": "BTC_USDT", "amount": -1,
                        "price_offset": "0.5", "activation_price": "0"})
# business code check
detail = (s_trail.detail or {}).get("order") or {}
if isinstance(detail, dict) and detail.get("code") not in (None, 0, "0"):
    rec("trail_business_ok", False, detail)
else:
    rec("trail_business_ok", s_trail.ok, detail.get("id", ""))

print("\n=== 7 flatten / cancel ===")
run("flatten", {"action": "flatten", "symbol": "BTC_USDT", "label": "t-f"})
run("cancel_all", {"action": "cancel_all", "symbol": "BTC_USDT"})
run("cancel_price_all", {"action": "cancel_price_all", "symbol": "BTC_USDT"})

print("\n=== 8 inbox file flow ===")
tmp = ROOT / ".smoke_inbox"
if tmp.exists():
    shutil.rmtree(tmp)
paths = ProjectPaths(tmp)
paths.ensure()
bot = BotConfig(bot_id="alpha", env="testnet", symbols=["BTC_USDT", "ETH_USDT"], max_notional_usd=500)
(paths.bot_inbox("alpha") / "a-hold.json").write_text(json.dumps({"action": "hold", "meta": {"signal_id": "f1"}}), encoding="utf-8")
(paths.bot_inbox("alpha") / "b-bad.json").write_text("{bad", encoding="utf-8")
(paths.bot_inbox("alpha") / "c-open.json").write_text(json.dumps({
    "action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit",
    "price": round(last * 0.97, 1), "label": "t-file",
}), encoding="utf-8")
stats = run_bot_once(bot, paths)
rec("inbox_once", stats.get("picked") == 3 and stats.get("ok") == 2 and stats.get("failed") == 1, stats)
rec("inbox_done_names", (paths.bot_done("alpha") / "a-hold.json").exists() and (paths.bot_done("alpha") / "c-open.json").exists())
rec("inbox_failed_name", (paths.bot_failed("alpha") / "b-bad.json").exists())
rec("inbox_sidecars", (paths.bot_done("alpha") / "a-hold.json.result.json").exists()
    and (paths.bot_failed("alpha") / "b-bad.json.error.json").exists())

print("\n=== 9 official single close ===")
if not c.is_dual_position_mode():
    try:
        c.place_order({"contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc", "text": "t-o"})
        r = c.place_order({"contract": "BTC_USDT", "size": 0, "close": True, "price": "0", "tif": "ioc",
                           "reduce_only": True, "text": "t-oc"})
        rec("official_single_close", r.get("is_close") is True, r.get("id"))
    except GateApiError as e:
        rec("official_single_close", False, str(e))
else:
    rec("official_dual_close", True, "skip close=true in dual")

print("\n=== 10 final cleanup ===")
run("flatten2", {"action": "flatten", "symbol": "BTC_USDT"})
run("cancel_all2", {"action": "cancel_all", "symbol": "BTC_USDT"})
run("cancel_po2", {"action": "cancel_price_all", "symbol": "BTC_USDT"})
for sym in ("BTC_USDT", "ETH_USDT"):
    try:
        c.place_order({"contract": sym, "size": 8, "price": "0", "tif": "ioc", "reduce_only": True, "text": "t-fS"})
        c.place_order({"contract": sym, "size": -8, "price": "0", "tif": "ioc", "reduce_only": True, "text": "t-fL"})
    except GateApiError:
        pass

print("\n===== SUMMARY =====")
npass = sum(1 for r in results if r[1] == "PASS")
nfail = sum(1 for r in results if r[1] == "FAIL")
print("PASS", npass, "FAIL", nfail)
for name, st, detail in results:
    if st == "FAIL":
        print(" ", name, detail)
print("leftover pos", [(p.get("contract"), p.get("mode"), p.get("size")) for p in c.get_positions() if int(p.get("size") or 0)])
print("leftover orders", len(c.list_orders()), "po", len(c.list_price_orders()))
