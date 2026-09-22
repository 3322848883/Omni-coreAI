# -*- coding: utf-8 -*-
"""Place all Gate order types on testnet and leave them for UI verification."""
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
print("LAST", last)


def run(name, payload):
    rep = ex.execute_signal(parse_signal(payload))
    s = rep.results[0]
    o = (s.detail or {}).get("order") or {}
    print("%s | ok=%s | id=%s | px=%s | tif=%s | sz=%s | err=%s" % (
        name, s.ok, o.get("id"), o.get("price"), o.get("tif"), o.get("size"), (s.error or "")[:120]))
    for k in ("tp_orders", "sl_orders"):
        for x in (s.detail or {}).get(k) or []:
            print("   %s id=%s" % (k, x.get("id")))


run("LIMIT_LONG", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit",
                   "price": round(last * 0.97, 1), "label": "all-limit-long"})
run("LIMIT_SHORT", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "limit",
                    "price": round(last * 1.03, 1), "label": "all-limit-short"})
run("POSTONLY_LONG", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "post_only",
                      "price": round(last * 0.98, 1), "label": "all-po-long"})
run("POSTONLY_SHORT", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "post_only",
                       "price": round(last * 1.02, 1), "label": "all-po-short"})
run("MKT_TP_SL", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                  "label": "all-tpsl", "tp": round(last * 1.05, 1), "sl": round(last * 0.95, 1),
                  "trigger_price_type": "mark"})
run("COND_LONG", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                  "trigger_price": round(last * 1.03, 1), "label": "all-cond-long"})
run("COND_SHORT", {"action": "open_short", "symbol": "BTC_USDT", "size": 1, "type": "market",
                   "trigger_price": round(last * 0.97, 1), "label": "all-cond-short"})
run("IOC_FAR", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "ioc",
                "price": round(last * 0.90, 1), "label": "all-ioc"})
run("FOK_FAR", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "fok",
                "price": round(last * 0.90, 1), "label": "all-fok"})
run("TRAIL", {"action": "trail", "symbol": "BTC_USDT", "amount": -1, "price_offset": "0.5",
              "activation_price": "0"})

print("=== OPEN ORDERS ===")
print(json.dumps([{k: o.get(k) for k in ("id", "size", "price", "left", "tif", "status", "text")}
                  for o in c.list_orders("BTC_USDT")], ensure_ascii=False, indent=2))
print("=== PRICE ORDERS ===")
for p in c.list_price_orders("BTC_USDT"):
    tr, ini = p.get("trigger") or {}, p.get("initial") or {}
    print(json.dumps({"id": p.get("id"), "status": p.get("status"), "trigger_px": tr.get("price"),
                      "rule": tr.get("rule"), "ord_px": ini.get("price"), "size": ini.get("size"),
                      "text": ini.get("text")}, ensure_ascii=False))
print("=== POS ===", [(p["mode"], p["size"]) for p in c.get_positions() if int(p.get("size") or 0) != 0])
