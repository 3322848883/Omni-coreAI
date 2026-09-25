# -*- coding: utf-8 -*-
"""M19 small live closed-loop + cleanup for prelaunch runner."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.executor import Executor
from gate_bot.schema import parse_signal
from gate_bot.tradelog import TradeLogger, trade_log_path


def _pick_symbol(client) -> str:
    """Pick a liquid contract whose 1-contract notional is <= 10 USD if possible."""
    candidates = ["DOGE_USDT", "BNB_USDT", "ADA_USDT", "XRP_USDT", "BTC_USDT", "ETH_USDT"]
    best = ("BTC_USDT", 1e9)
    for s in candidates:
        try:
            cm = client.get_contract(s)
            last = client.get_last_price(s)
            nom = float(last) * float(cm.quanto_multiplier or 1)
            if nom <= 10.0:
                return s
            if nom < best[1]:
                best = (s, nom)
        except Exception:  # noqa: BLE001
            continue
    return best[0]


def run_live(REP, gate_client) -> None:
    if not os.environ.get("GATE_API_KEY"):
        REP.rec("M19", "live_closed_loop", False, "SKIP no GATE_API_KEY")
        return
    client = gate_client("live")
    symbol = _pick_symbol(client)
    cm = client.get_contract(symbol)
    last = client.get_last_price(symbol)
    nom = float(last) * float(cm.quanto_multiplier or 1)
    size_usd = min(10.0, max(nom, 8.0))  # ≥1 contract, ≤10U
    if nom > 10.0:
        REP.rec("M19", "min_notional_note", True,
                f"{symbol} 1-contract≈{nom:.2f}U >10U; using size=1 anyway")
        size = 1
        size_usd = None
    else:
        size = None

    ex = Executor(
        client,
        symbols_whitelist=[symbol],
        max_notional_usd=10,
        require_sl=True,
        order_scope="own",
    )
    body = {
        "action": "open_long",
        "symbol": symbol,
        "type": "market",
        "tp": round(last * 1.004, 6),
        "sl": round(last * 0.996, 6),
        "tp_mode": "trigger",
        "sl_mode": "trigger",
        "tp_type": "market",
        "sl_type": "market",
        "trigger_price_type": "mark",
        "label": "prelaunch",
        "meta": {"signal_id": "prelaunch-m19", "kind": "small_probe"},
    }
    if size is not None:
        body["size"] = size
    else:
        body["size_usd"] = size_usd

    try:
        rep = ex.execute_signal(parse_signal(body))
        s = rep.results[0] if rep.results else None
        REP.rec("M19", "entry_ok", rep.ok, (s.error if s and not rep.ok else "filled-ish")[:80])
        if not rep.ok:
            REP.problem("M19-entry", (s.error if s else "no step"), level="P0")
            return
        chk_e = s.detail.get("order_check") or {}
        tp = ((s.detail.get("tp_orders") or [{}])[0].get("check") or {})
        sl = ((s.detail.get("sl_orders") or [{}])[0].get("check") or {})
        legs = bool(chk_e.get("confirmed") and tp.get("confirmed") and sl.get("confirmed"))
        REP.rec("M19", "three_legs", legs,
                f"e={chk_e.get('confirmed')} tp={tp.get('confirmed')} sl={sl.get('confirmed')}",
                evidence=str(chk_e.get("id") or ""))
        jpath = trade_log_path(ROOT, "prelaunch")
        TradeLogger(jpath).log_execution("prelaunch", {"plan_cycle": "prelaunch-m19"},
                                         rep.to_dict(), source="prelaunch_live")
        rows = TradeLogger(jpath).tail(5)
        REP.rec("M19", "journal", any(r.get("type") == "execution" for r in rows), f"rows={len(rows)}")
    except Exception as e:  # noqa: BLE001
        REP.rec("M19", "entry_ok", False, str(e)[:100])
        REP.problem("M19-exc", str(e), level="P0")
    finally:
        for action in ("flatten", "cancel_price_all", "cancel_all"):
            try:
                ex.execute_signal(parse_signal({"action": action, "symbol": symbol}))
            except Exception:  # noqa: BLE001
                pass
        # residual check
        try:
            pos = [p for p in (client.get_positions() or []) if p.get("contract") == symbol and int(p.get("size") or 0) != 0]
            po = client.list_price_orders(symbol) or []
            REP.rec("M19", "no_residual", len(pos) == 0, f"positions={len(pos)} price_orders={len(po)}")
        except Exception as e:  # noqa: BLE001
            REP.rec("M19", "no_residual", False, str(e)[:80])
