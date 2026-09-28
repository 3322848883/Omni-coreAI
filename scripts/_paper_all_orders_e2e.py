# -*- coding: utf-8 -*-
"""模拟盘全订单类型 e2e：确保每类订单真实可用。

覆盖：市价开、限价开、突破入场、单TP/SL、双TP、modify_tp_sl、
减仓、平仓、tp_mode=limit_order、cancel_price_all、hold。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.executor import Executor
from gate_bot.paper.exchange import PaperExchange
from gate_bot.paper.engine import PaperEngine
from gate_bot.paper.store import PaperStore
from gate_bot.schema import parse_signal

PASS, FAIL = [], []


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
        self.spread = 10.0

    def _p(self, symbol):
        return self.px.get(symbol, 83000.0)

    def get_last_price(self, symbol):
        return self._p(symbol)

    def get_orderbook_top(self, symbol, limit=5):
        p = self._p(symbol)
        half = max(self.spread / 2, p * 0.00005)
        return {"bids": [{"p": p - half, "s": 100}], "asks": [{"p": p + half, "s": 100}]}

    def get_ticker(self, symbol):
        return {"last": self._p(symbol), "mark_price": self._p(symbol), "funding_rate": 0.0001}

    def get_contract(self, symbol):
        return FakeMeta()

    def get_klines(self, *a, **k):
        return []

    def get_contract_stats(self, *a, **k):
        return []


def check(name: str, cond: bool, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), name, detail)
    return cond


def run():
    td = tempfile.mkdtemp(prefix="paper-e2e-")
    store = PaperStore(Path(td) / "account.db")
    store.init_config({"initial_capital": "10000", "leverage": "20", "fee_rate": "0.0005",
                       "funding_enabled": "0", "position_mode": "single"})
    feed = FakeFeed()
    client = PaperExchange(
        env="paper", api_key="", api_secret="",
        store_path=Path(td) / "account.db", feed=feed,
        config={
            "initial_capital": 10000, "leverage": 20, "fee_rate": 0.0005,
            "funding_enabled": False, "position_mode": "single", "margin_mode": "isolated",
            "price_band_pct": 5.0, "maintenance_margin_rate": 0.005,
            "trigger_price_type": "latest", "feed_exchange": "fake",
        },
    )
    ex = Executor(
        client,
        symbols_whitelist=["BTC_USDT", "ETH_USDT"],
        max_notional_usd=200000,
        require_sl=True,
        label_prefix="e2e",
        account_risk={"max_notional_pct": 20, "risk_pct": 0.01},
        order_scope="own",
    )

    # 1) 市价开多 + 单 TP/SL
    r = ex.execute_signal(parse_signal({
        "action": "open_long", "symbol": "BTC_USDT", "size_usd": 2000,
        "tp": 84000, "sl": 82000, "leverage": 20,
    }))
    check("1 open_long market + tp/sl", r.ok, str(r.to_dict())[:120])
    pos = [p for p in store.get_positions("BTC_USDT") if abs(float(p.get("size") or 0)) > 0]
    check("1 position open", len(pos) == 1 and float(pos[0]["size"]) > 0, str(pos)[:100])

    # 2) modify_tp_sl 移保护
    r = ex.execute_signal(parse_signal({
        "action": "modify_tp_sl", "symbol": "BTC_USDT", "sl": 82500, "tp": 83800,
    }))
    check("2 modify_tp_sl", r.ok, str(r.to_dict())[:100])

    # 3) 平多
    r = ex.execute_signal(parse_signal({
        "action": "close", "symbol": "BTC_USDT", "side": "long",
    }))
    check("3 close long", r.ok, str(r.to_dict())[:150])
    pos = [p for p in client.get_positions() or [] if abs(float(p.get("size") or 0)) > 0
           and p.get("contract") == "BTC_USDT"]
    check("3 flat after close", not pos)

    # 4) 市价开空 + 双TP
    r = ex.execute_signal(parse_signal({
        "action": "open_short", "symbol": "BTC_USDT",
        "size_usd": 1500, "tp": 82000, "tp2": 81500, "tp1_share": 0.5, "sl": 83800,
        "leverage": 20,
    }))
    check("4 open_short market + dual tp", r.ok, str(r.to_dict())[:150])
    step = r.results[0]
    tps = (step.detail or {}).get("tp_orders") or []
    pxs = []
    for t in tps:
        if isinstance(t, dict):
            o = t.get("order") or t
            pxs.append(float(o.get("trigger_price") or (o.get("trigger") or {}).get("price") or 0))
    check("4 dual tp prices present",
          len(tps) >= 2 and any(abs(x - 82000) < 2 for x in pxs) and any(abs(x - 81500) < 2 for x in pxs),
          f"tp_legs={len(tps)} px={pxs}")

    # 5) reduce_short 减仓
    r = ex.execute_signal(parse_signal({
        "action": "reduce_short", "symbol": "BTC_USDT", "size": 10,
    }))
    check("5 reduce_short", r.ok, str(r.to_dict())[:150])

    # 6) stop_entry_long
    r = ex.execute_signal(parse_signal({
        "action": "stop_entry_long", "symbol": "BTC_USDT", "trigger_price": 84000,
        "size_usd": 1000, "tp": 85000, "sl": 83500, "leverage": 20,
    }))
    check("6 stop_entry_long + hang exits", r.ok, str(r.to_dict())[:150])

    # 7) tp_mode limit_order
    r = ex.execute_signal(parse_signal({
        "action": "open_long", "symbol": "ETH_USDT", "size_usd": 500,
        "price": 2700, "type": "limit", "tp": 2750, "sl": 2650,
        "tp_mode": "limit_order", "leverage": 10,
    }))
    check("7 open limit + tp_mode limit_order", r.ok, str(r.to_dict())[:120])

    # 8) cancel_price_all (own)
    r = ex.execute_signal(parse_signal({
        "action": "cancel_price_all", "symbol": "ETH_USDT", "label": "e2e",
    }))
    check("8 cancel_price_all own", r.ok, str(r.to_dict())[:100])

    # 9) close_all / flatten
    r = ex.execute_signal(parse_signal({"action": "close_all", "symbol": "BTC_USDT"}))
    check("9 close_all BTC", r.ok, str(r.to_dict())[:80])
    pos = [p for p in store.get_positions() if abs(float(p.get("size") or 0)) > 0]
    check("9 all flat", not pos, str(pos)[:80])

    # 10) hold no-op
    r = ex.execute_signal(parse_signal({"action": "hold"}))
    check("10 hold noop", r.ok and r.results[0].action == "hold")

    # 11) require_sl 拒裸开
    r = ex.execute_signal(parse_signal({
        "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
    }))
    check("11 naked open rejected", (not r.ok) and "SL_REQUIRED" in (r.results[0].error or ""))

    # 12) 双 TP 数量：再开一仓数 price_orders
    r = ex.execute_signal(parse_signal({
        "action": "open_long", "symbol": "BTC_USDT", "size_usd": 3000,
        "tp": 83500, "tp2": 84200, "tp1_share": 0.5, "sl": 82000, "leverage": 20,
    }))
    step = r.results[0]
    tps = (step.detail or {}).get("tp_orders") or []
    sls = (step.detail or {}).get("sl_orders") or []
    check("12 dual tp hung", r.ok and len(tps) >= 2 and len(sls) >= 1,
          f"tp={len(tps)} sl={len(sls)}")

    # 13) 三级止盈
    r = ex.execute_signal(parse_signal({
        "action": "open_long", "symbol": "ETH_USDT", "size_usd": 900,
        "tp": 2720, "tp2": 2750, "tp3": 2800,
        "tp1_share": 0.3, "tp2_share": 0.3,
        "sl": 2650, "leverage": 10,
    }))
    step = r.results[0]
    tps = (step.detail or {}).get("tp_orders") or []
    check("13 triple tp hung", r.ok and len(tps) == 3, f"tp={len(tps)} err={step.error}")

    # 14) 多头网格 3 层
    r = ex.execute_signal(parse_signal({
        "action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
        "levels": [
            {"price": 82000, "size_usd": 400},
            {"price": 81500, "size_usd": 400},
            {"price": 81000, "size_usd": 400},
        ],
        "tp": 83000, "sl": 80000, "leverage": 20,
        "tp_scope": "shared", "sl_scope": "shared",
    }))
    n_open = sum(1 for o in (client.list_orders("BTC_USDT") or []) if int(o.get("size") or 0) > 0)
    check("14 grid long 3 levels", r.ok and n_open >= 3, f"ok={r.ok} open_long={n_open} err={r.results[0].error if r.results else ''}")

    # 15) 空头网格 3 层
    r = ex.execute_signal(parse_signal({
        "action": "grid", "symbol": "ETH_USDT", "side": "short", "type": "limit",
        "levels": [
            {"price": 2780, "size_usd": 300},
            {"price": 2800, "size_usd": 300},
            {"price": 2820, "size_usd": 300},
        ],
        "tp": 2700, "sl": 2880, "leverage": 10,
        "tp_scope": "shared", "sl_scope": "shared",
    }))
    n_open_s = sum(1 for o in (client.list_orders("ETH_USDT") or []) if int(o.get("size") or 0) < 0)
    check("15 grid short 3 levels", r.ok and n_open_s >= 3,
          f"ok={r.ok} open_short={n_open_s} err={r.results[0].error if r.results else ''}")

    # 16) 双向网格：多 + 空 两侧各 2 层
    r1 = ex.execute_signal(parse_signal({
        "action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
        "levels": [
            {"price": 80500, "size_usd": 300},
            {"price": 80000, "size_usd": 300},
        ],
        "tp": 81500, "sl": 79000, "leverage": 20, "tp_scope": "shared", "sl_scope": "shared",
    }))
    r2 = ex.execute_signal(parse_signal({
        "action": "grid", "symbol": "BTC_USDT", "side": "short", "type": "limit",
        "levels": [
            {"price": 84500, "size_usd": 300},
            {"price": 85000, "size_usd": 300},
        ],
        "tp": 83500, "sl": 86000, "leverage": 20, "tp_scope": "shared", "sl_scope": "shared",
    }))
    all_orders = client.list_orders("BTC_USDT") or []
    n_l = sum(1 for o in all_orders if int(o.get("size") or 0) > 0)
    n_s = sum(1 for o in all_orders if int(o.get("size") or 0) < 0)
    check("16 dual grid long+short", r1.ok and r2.ok and n_l >= 2 and n_s >= 2,
          f"long={n_l} short={n_s}")

    # 17) 网格 + 三级止盈 shared（最后一腿挂三级）
    r = ex.execute_signal(parse_signal({
        "action": "grid", "symbol": "ETH_USDT", "side": "long", "type": "limit",
        "levels": [
            {"price": 2650, "size_usd": 200},
            {"price": 2630, "size_usd": 200},
            {"price": 2610, "size_usd": 200},
        ],
        "tp": 2700, "tp2": 2730, "tp3": 2780,
        "tp1_share": 0.34, "tp2_share": 0.33,
        "sl": 2550, "leverage": 10, "tp_scope": "shared", "sl_scope": "shared",
    }))
    check("17 grid + triple tp", r.ok, str(r.to_dict())[:120])

    print("\n==== SUMMARY ====")
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("failed:", FAIL)
        return 2
    print("ALL ORDER TYPES OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
