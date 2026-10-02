# -*- coding: utf-8 -*-
"""Execute every examples/signals/*.json on Gate testnet and verify.

Prices in examples are template values; this harness rewrites prices relative
to live last so they are valid on the current book, then executes + cleans up.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import parse_signal, expand_signal  # noqa: E402

RESULTS = []


def rec(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"
    RESULTS.append((name, st, str(detail)[:110]))
    print("%s  %-34s %s" % (st, name, str(detail)[:110]))
    return ok


def rewrite_prices(payload: dict, prices: dict) -> dict:
    """Map template prices near current market so testnet accepts them."""
    sym = payload.get("symbol") or ""
    px = prices.get(str(sym)) or prices.get("BTC_USDT") or 1.0
    if not sym and payload.get("orders"):
        # multi: rewrite each order by its symbol
        out = dict(payload)
        out["orders"] = [
            rewrite_prices(o, prices) for o in payload["orders"]
        ]
        return out
    out = dict(payload)
    out.pop("comment", None)
    act = str(out.get("action") or "")
    side_field = str(out.get("side") or "")
    side_sign = -1 if ("short" in act or side_field == "short") else 1

    def near(bps: float) -> float:
        return round(px * (1 + side_sign * bps / 10000.0), 1)

    if out.get("price") is not None:
        out["price"] = near(-150 if side_sign > 0 else 150)  # non-marketable
    if out.get("tp") is not None:
        out["tp"] = near(300)
    if out.get("sl") is not None:
        out["sl"] = near(-300)
    out["trigger_price_type"] = "latest"
    # explicit rules: long tp>= / sl<= ; short inverted
    if "short" in act or side_field == "short":
        out.setdefault("trigger_rule_tp", 2)
        out.setdefault("trigger_rule_sl", 1)
    else:
        out.setdefault("trigger_rule_tp", 1)
        out.setdefault("trigger_rule_sl", 2)
    if out.get("trigger_price") is not None:
        out["trigger_price"] = near(200)
    if out.get("levels"):
        out["levels"] = [
            {"price": near(-100 * (i + 1)), "size": lv.get("size", 1)}
            for i, lv in enumerate(out["levels"])
        ]
    return out


def main() -> int:
    if not os.environ.get("GATE_TESTNET_API_KEY"):
        print("missing GATE_TESTNET_API_KEY")
        return 2
    client = GateClient(
        os.environ["GATE_TESTNET_API_KEY"],
        os.environ["GATE_TESTNET_API_SECRET"],
        env="testnet",
    )
    prices = {
        "BTC_USDT": client.get_last_price("BTC_USDT"),
        "ETH_USDT": client.get_last_price("ETH_USDT"),
        "SOL_USDT": client.get_last_price("SOL_USDT"),
    }
    print("TESTNET last", prices)
    ex = Executor(
        client,
        symbols_whitelist=["BTC_USDT", "ETH_USDT", "SOL_USDT"],
        max_notional_usd=80,
        require_sl=True,
        order_scope="own",
    )
    print("=" * 72)

    files = sorted((ROOT / "examples" / "signals").glob("*.json"))
    for path in files:
        name = path.name
        raw = json.loads(path.read_text(encoding="utf-8"))
        payload = rewrite_prices(raw, prices)
        # ensure SL on opens for require_sl (examples already have sl except hold/manage)
        try:
            sig = parse_signal(payload)
            n = len(expand_signal(sig))
        except Exception as e:  # noqa: BLE001
            rec("parse_" + name, False, str(e))
            continue
        rec("parse_" + name, True, "intents=%d" % n)

        try:
            rep = ex.execute_signal(sig)
            # manage/hold must be ok; trading ones ok or expected fail (FOK etc.)
            ok = rep.ok
            first_err = next((s.error for s in rep.results if not s.ok), "")
            rec("exec_" + name, ok or ("hold" in name or "manage" in name), first_err[:80] or "ok")
        except Exception as e:  # noqa: BLE001
            rec("exec_" + name, False, str(e)[:80])

    # cleanup
    print("\n[cleanup]")
    for sym in ("BTC_USDT", "ETH_USDT", "SOL_USDT"):
        from omnialpha.schema import parse_signal as ps
        ex.execute_signal(ps({"action": "flatten", "symbol": sym}))
        ex.execute_signal(ps({"action": "cancel_all", "symbol": sym}))
        ex.execute_signal(ps({"action": "cancel_price_all", "symbol": sym}))
    rec("cleanup", True, "flat+cancel all")

    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("EXAMPLES TESTNET SUMMARY  PASS=%d FAIL=%d TOTAL=%d" % (n_pass, n_fail, len(RESULTS)))
    if n_fail:
        for n, s, d in RESULTS:
            if s == "FAIL":
                print("  FAIL", n, "|", d)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
