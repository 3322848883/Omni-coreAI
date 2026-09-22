# -*- coding: utf-8 -*-
"""Probe official Gate Futures API under DUAL (hedge) position mode."""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

KEY = "7b31134e731b10ea859da2e75440d263"
SEC = "c352fbb46779a1861d2b1facfa33e0d20306785bc1d1bef768e8951a61213b87"
BASE = "https://api-testnet.gateapi.io"


def call(method, path, qs="", body=None):
    body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
    body_hash = hashlib.sha512(body_str.encode()).hexdigest()
    ts = str(int(time.time()))
    sign = hmac.new(SEC.encode(), f"{method}\n{path}\n{qs}\n{body_hash}\n{ts}".encode(), hashlib.sha512).hexdigest()
    headers = {"KEY": KEY, "SIGN": sign, "Timestamp": ts, "Accept": "application/json", "Content-Type": "application/json"}
    url = BASE + path + (f"?{qs}" if qs else "")
    data = body_str.encode() if body_str else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw.strip() else {"raw": raw[:180]}
        except Exception:
            return e.code, {"raw": raw[:180]}
    except Exception as e:
        return 0, {"err": str(e)}


print("=== 0 mode ===")
st, acc = call("GET", "/api/v4/futures/usdt/accounts")
print("accounts", st, "position_mode=", acc.get("position_mode"), "in_dual_mode=", acc.get("in_dual_mode"))

print("=== 1 dual_comp positions ===")
print(call("GET", "/api/v4/futures/usdt/dual_comp/positions/BTC_USDT"))

print("=== 2 dual leverage path with real body ===")
print("dual lev 5x", call("POST", "/api/v4/futures/usdt/dual_comp/positions/BTC_USDT/leverage", "", {"leverage": "5"}))

print("=== 3 open long+short both books ===")
st, o1 = call("POST", "/api/v4/futures/usdt/orders", "", {
    "contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc", "text": "t-dual-long",
})
print("long ", st, o1.get("id"), o1.get("size"), o1.get("biz_info"))
st, o2 = call("POST", "/api/v4/futures/usdt/orders", "", {
    "contract": "BTC_USDT", "size": -1, "price": "0", "tif": "ioc", "text": "t-dual-short",
})
print("short", st, o2.get("id"), o2.get("size"), o2.get("biz_info"))

print("=== 4 dual books after both opens ===")
st, dpos = call("GET", "/api/v4/futures/usdt/dual_comp/positions/BTC_USDT")
for p in dpos or []:
    if int(p.get("size") or 0):
        print(" ", p.get("mode"), "size", p.get("size"), "entry", p.get("entry_price"))

print("=== 5 official dual reduce sign ===")
# docs: dual reduce_only=true, size positive = reduce short, negative = reduce long
st, r1 = call("POST", "/api/v4/futures/usdt/orders", "", {
    "contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc",
    "reduce_only": True, "text": "t-dual-reduce-pos",
})
print("reduce +1 (expect short)", st, r1.get("id"), r1.get("is_reduce_only"))
st, dpos = call("GET", "/api/v4/futures/usdt/dual_comp/positions/BTC_USDT")
for p in dpos or []:
    if int(p.get("size") or 0):
        print("  after +1:", p.get("mode"), p.get("size"))

print("=== 6 dual close size=0 + auto_size ===")
for side in ("long", "short"):
    st, c = call("POST", "/api/v4/futures/usdt/orders", "", {
        "contract": "BTC_USDT", "size": 0, "close": True, "price": "0", "tif": "ioc",
        "reduce_only": True, "auto_size": side, "text": f"t-dual-close-{side}",
    })
    print(f"close auto_size={side}", st, c.get("id"), c.get("is_close"), str(c)[:140])

print("=== 7 leftover ===")
print("orders", call("GET", "/api/v4/futures/usdt/orders", "status=open")[1])
st, dpos = call("GET", "/api/v4/futures/usdt/dual_comp/positions/BTC_USDT")
print("pos", [(p.get("mode"), p.get("size")) for p in dpos or [] if int(p.get("size") or 0)])
