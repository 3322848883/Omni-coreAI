# -*- coding: utf-8 -*-
"""网格 N 层压力测试：3/5/10/20/50 层均应成功挂出。"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.executor import Executor
from gate_bot.paper.exchange import PaperExchange
from gate_bot.schema import parse_signal


class FakeMeta:
    quanto_multiplier = 0.0001
    order_size_round = 1.0
    order_price_round = 0.1
    leverage_max = 100
    min_notional_usd = 8.0


class FakeFeed:
    name = "fake"

    def __init__(self):
        self.px = {"BTC_USDT": 83000.0, "ETH_USDT": 2700.0}

    def get_last_price(self, symbol):
        return self.px.get(symbol, 83000.0)

    def get_orderbook_top(self, symbol, limit=5):
        p = self.get_last_price(symbol)
        return {"bids": [{"p": p - 5, "s": 100}], "asks": [{"p": p + 5, "s": 100}]}

    def get_ticker(self, symbol):
        p = self.get_last_price(symbol)
        return {"last": p, "mark_price": p, "funding_rate": 0.0001}

    def get_contract(self, symbol):
        return FakeMeta()

    def get_klines(self, *a, **k):
        return []

    def get_contract_stats(self, *a, **k):
        return []


def make_ex(td):
    client = PaperExchange(
        env="paper", api_key="", api_secret="",
        store_path=Path(td) / "account.db", feed=FakeFeed(),
        config={
            "initial_capital": 100000, "leverage": 20, "fee_rate": 0.0005,
            "funding_enabled": False, "position_mode": "single", "margin_mode": "isolated",
            "price_band_pct": 50.0, "maintenance_margin_rate": 0.005,
            "trigger_price_type": "latest", "feed_exchange": "fake",
        },
    )
    ex = Executor(
        client, symbols_whitelist=["BTC_USDT", "ETH_USDT"],
        max_notional_usd=1e7, require_sl=True, label_prefix="ng",
        account_risk={"max_notional_pct": 50, "risk_pct": 0.01},
        order_scope="own",
    )
    return client, ex


def main():
    fails = []
    for n in (3, 5, 10, 20, 50):
        td = tempfile.mkdtemp(prefix=f"grid{n}-")
        client, ex = make_ex(td)
        base = 80000.0
        levels = [
            {"price": base - i * 50, "size_usd": 100}
            for i in range(n)
        ]
        r = ex.execute_signal(parse_signal({
            "action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
            "levels": levels, "tp": 82000, "sl": 78000, "leverage": 20,
            "tp_scope": "shared", "sl_scope": "shared",
        }))
        n_open = sum(1 for o in (client.list_orders("BTC_USDT") or []) if int(o.get("size") or 0) > 0)
        ok = r.ok and n_open == n
        print(f"grid N={n:2d}  ok={r.ok}  open_orders={n_open}/{n}  "
              f"{'PASS' if ok else 'FAIL ' + str(r.results[0].error if r.results else '')}")
        if not ok:
            fails.append(n)

    # 双向 10+10
    td = tempfile.mkdtemp(prefix="griddual-")
    client, ex = make_ex(td)
    lv = [{"price": 80000 - i * 40, "size_usd": 80} for i in range(10)]
    r1 = ex.execute_signal(parse_signal({
        "action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
        "levels": lv, "tp": 81000, "sl": 79000, "leverage": 20,
        "tp_scope": "shared", "sl_scope": "shared",
    }))
    lv2 = [{"price": 86000 + i * 40, "size_usd": 80} for i in range(10)]
    r2 = ex.execute_signal(parse_signal({
        "action": "grid", "symbol": "BTC_USDT", "side": "short", "type": "limit",
        "levels": lv2, "tp": 85000, "sl": 87000, "leverage": 20,
        "tp_scope": "shared", "sl_scope": "shared",
    }))
    nl = sum(1 for o in (client.list_orders("BTC_USDT") or []) if int(o.get("size") or 0) > 0)
    ns = sum(1 for o in (client.list_orders("BTC_USDT") or []) if int(o.get("size") or 0) < 0)
    ok = r1.ok and r2.ok and nl == 10 and ns == 10
    print(f"dual 10+10  long={nl} short={ns}  {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("dual")

    # 超限 51 层应拒
    td = tempfile.mkdtemp(prefix="grid51-")
    client, ex = make_ex(td)
    lv = [{"price": 80000 - i, "size_usd": 10} for i in range(51)]
    try:
        r = ex.execute_signal(parse_signal({
            "action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
            "levels": lv, "tp": 81000, "sl": 79000,
        }))
        rejected = (not r.ok) or False
        # parse_signal itself may raise
    except Exception as e:
        rejected = True
        print("grid N=51 rejected at parse:", type(e).__name__, e)
    else:
        rejected = not r.ok
        print("grid N=51 ok=", r.ok, "-> expect reject")
    print("grid N=51", "PASS" if rejected else "FAIL accepted 51")
    if not rejected:
        fails.append("51")

    print("\n====", "ALL N-LAYER GRID OK" if not fails else f"FAIL {fails}")
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
