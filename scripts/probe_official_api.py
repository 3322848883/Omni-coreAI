# -*- coding: utf-8 -*-
"""Probe official Gate Futures API shapes on testnet before coding."""
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


print("=== 1 accounts path ===")
print("accounts ", call("GET", "/api/v4/futures/usdt/accounts")[0], str(call("GET", "/api/v4/futures/usdt/accounts")[1])[:160])
print("account  ", call("GET", "/api/v4/futures/usdt/account")[0])

print("=== 2 dual_mode / positions ===")
print("dual_mode GET", call("GET", "/api/v4/futures/usdt/dual_mode"))
print("positions    ", call("GET", "/api/v4/futures/usdt/positions")[0])
print("dual_comp    ", call("GET", "/api/v4/futures/usdt/dual_comp/positions/BTC_USDT")[0])

print("=== 3 leverage paths (empty body) ===")
print("single lev", call("POST", "/api/v4/futures/usdt/positions/BTC_USDT/leverage", "", {}))
print("dual   lev", call("POST", "/api/v4/futures/usdt/dual_comp/positions/BTC_USDT/leverage", "", {}))

print("=== 4 official close: size=0 + close=true ===")
code, order = call("POST", "/api/v4/futures/usdt/orders", "", {
    "contract": "BTC_USDT", "size": 1, "price": "0", "tif": "ioc", "text": "t-official-open",
})
print("open", code, order.get("id"), order.get("size"), order.get("status"))
code, closed = call("POST", "/api/v4/futures/usdt/orders", "", {
    "contract": "BTC_USDT", "size": 0, "close": True, "price": "0", "tif": "ioc",
    "reduce_only": True, "text": "t-official-close",
})
print("close", code, str(closed)[:250])

print("=== 5 leftover ===")
print("orders", call("GET", "/api/v4/futures/usdt/orders", "status=open")[1])
print("pos", [(p.get("mode"), p.get("size")) for p in (call("GET", "/api/v4/futures/usdt/positions")[1] or []) if int(p.get("size") or 0)])
