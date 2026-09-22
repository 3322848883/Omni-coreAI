# -*- coding: utf-8 -*-
"""Probe official Gate API multi-symbol under DUAL position mode."""
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
    print(f"  meta mult={meta.get('quanto_multiplier')} last={meta.get('last_price')}")

    st, oL = call("POST", "/api/v4/futures/usdt/orders", "", {
        "contract": sym, "size": 1, "price": "0", "tif": "ioc", "text": f"t-du-L-{sym}",
    })
    print("  long ", st, oL.get("id"), oL.get("size"), oL.get("biz_info") or oL.get("label") or oL.get("message"))

    st, oS = call("POST", "/api/v4/futures/usdt/orders", "", {
        "contract": sym, "size": -1, "price": "0", "tif": "ioc", "text": f"t-du-S-{sym}",
    })
    print("  short", st, oS.get("id"), oS.get("size"), oS.get("biz_info") or oS.get("label") or oS.get("message"))

    st, books = call("GET", f"/api/v4/futures/usdt/dual_comp/positions/{sym}")
    print("  books", [(p.get("mode"), p.get("size")) for p in (books or []) if int(p.get("size") or 0)])

    # cleanup dual: +1 reduce short, -1 reduce long
    st, r1 = call("POST", "/api/v4/futures/usdt/orders", "", {
        "contract": sym, "size": 1, "price": "0", "tif": "ioc", "reduce_only": True, "text": f"t-du-rS-{sym}",
    })
    st, r2 = call("POST", "/api/v4/futures/usdt/orders", "", {
        "contract": sym, "size": -1, "price": "0", "tif": "ioc", "reduce_only": True, "text": f"t-du-rL-{sym}",
    })
    print("  clean", r1.get("id"), r2.get("id"))

print("\n===== leftover =====")
print("orders", call("GET", "/api/v4/futures/usdt/orders", "status=open")[1])
st, pos = call("GET", "/api/v4/futures/usdt/positions")
print("pos", [(p.get("contract"), p.get("mode"), p.get("size")) for p in (pos or []) if int(p.get("size") or 0)])
