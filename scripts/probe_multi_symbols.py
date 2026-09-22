# -*- coding: utf-8 -*-
"""Probe official Gate API close/open on multiple contracts (testnet)."""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

KEY = "7b31134e731b10ea859da2e75440d263"
SEC = "c352fbb46779a1861d2b1facfa33e0d20306785bc1d1bef768e8951a61213b87"
BASE = "https://api-testnet.gateapi.io"
SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "XRP_USDT"]


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
            return e.code, json.loads(raw) if raw.strip() else {"raw": raw[:160]}
        except Exception:
            return e.code, {"raw": raw[:160]}
    except Exception as e:
        return 0, {"err": str(e)}


st, acc = call("GET", "/api/v4/futures/usdt/accounts")
print("MODE", acc.get("position_mode"), "in_dual=", acc.get("in_dual_mode"))

for sym in SYMBOLS:
    print(f"\n===== {sym} =====")
    st, meta = call("GET", f"/api/v4/futures/usdt/contracts/{sym}")
    print("contract", st, "mult", meta.get("quanto_multiplier"), "min", meta.get("order_size_min"), "last", meta.get("last_price"))

    st, o = call("POST", "/api/v4/futures/usdt/orders", "", {
        "contract": sym, "size": 1, "price": "0", "tif": "ioc", "text": f"t-multi-{sym}",
    })
    print("open ", st, o.get("id"), o.get("size"), o.get("status") or o.get("label") or o.get("message"))

    st, c = call("POST", "/api/v4/futures/usdt/orders", "", {
        "contract": sym, "size": 0, "close": True, "price": "0", "tif": "ioc",
        "reduce_only": True, "text": f"t-multi-close-{sym}",
    })
    print("close", st, c.get("id"), "is_close", c.get("is_close"), c.get("label") or c.get("message") or "")

print("\n===== leftover =====")
print("orders", call("GET", "/api/v4/futures/usdt/orders", "status=open")[1])
st, pos = call("GET", "/api/v4/futures/usdt/positions")
print("pos", [(p.get("contract"), p.get("mode"), p.get("size")) for p in (pos or []) if int(p.get("size") or 0)])
