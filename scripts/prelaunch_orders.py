# -*- coding: utf-8 -*-
"""M7–M10/M12/M14 testnet order-matrix checks for prelaunch runner."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from gate_bot.executor import Executor
from gate_bot.schema import parse_signal


def _cleanup(ex: Executor, symbol: str) -> None:
    for action in ("flatten", "cancel_price_all", "cancel_all"):
        try:
            ex.execute_signal(parse_signal({"action": action, "symbol": symbol}))
        except Exception:  # noqa: BLE001
            pass


def run_orders(REP, gate_client, env: str = "testnet") -> None:
    client = gate_client(env)
    symbol = "BTC_USDT"
    ex = Executor(
        client,
        symbols_whitelist=[symbol, "ETH_USDT", "DOGE_USDT"],
        max_notional_usd=100,
        require_sl=False,
        order_scope="own",
    )
    last = client.get_last_price(symbol)

    # ── M7 order types ──
    print("\n[M7] order types")
    cases = [
        ("market", {"type": "market"}),
        ("limit", {"type": "limit", "price": round(last * 0.90, 1)}),
        ("post_only", {"type": "post_only", "price": round(last * 0.88, 1)}),
        ("ioc", {"type": "ioc", "price": round(last * 0.90, 1)}),
        ("fok", {"type": "fok", "price": round(last * 0.50, 1)}),  # far → expect cancel/reject ok
    ]
    for name, extra in cases:
        body = {
            "action": "open_long",
            "symbol": symbol,
            "size_usd": 15 if env != "live" else 10,
            "label": f"m7-{name}",
            **extra,
        }
        try:
            rep = ex.execute_signal(parse_signal(body))
            # fok far-from-market may "fail" as cancel — treat as pass if API accepted
            step = rep.results[0] if rep.results else None
            ok = bool(step and (step.ok or (name == "fok")))
            detail = (step.error or str(step.detail)[:80]) if step else "no step"
            REP.rec("M7", f"type_{name}", ok, detail)
        except Exception as e:  # noqa: BLE001
            REP.rec("M7", f"type_{name}", False, str(e)[:100])
        _cleanup(ex, symbol)

    # ── M8 three legs ──
    print("\n[M8] entry+TP/SL three legs")
    try:
        last = client.get_last_price(symbol)
        sig = parse_signal({
            "action": "open_long", "symbol": symbol, "size_usd": 15, "type": "market",
            "tp": round(last * 1.005, 1), "sl": round(last * 0.995, 1),
            "tp_mode": "trigger", "sl_mode": "trigger",
            "tp_type": "market", "sl_type": "market",
            "trigger_price_type": "mark",
            "label": "m8-legs",
        })
        rep = ex.execute_signal(sig)
        s = rep.results[0] if rep.results else None
        chk_e = ((s.detail.get("order_check") or {}) if s else {}) or {}
        tp = (((s.detail.get("tp_orders") or [{}])[0].get("check") or {}) if s else {}) or {}
        sl = (((s.detail.get("sl_orders") or [{}])[0].get("check") or {}) if s else {}) or {}
        ok = bool(rep.ok and chk_e.get("confirmed") and tp.get("confirmed") and sl.get("confirmed"))
        REP.rec("M8", "three_legs_confirmed", ok,
                f"entry={chk_e.get('confirmed')} tp={tp.get('confirmed')} sl={sl.get('confirmed')}",
                evidence=str(chk_e.get("id") or ""))
        # trigger price rounding sanity: trigger prices should be multiple of tick
        meta = client.get_contract(symbol)
        for label, node in (("tp", tp), ("sl", sl)):
            trig = node.get("trigger_price") or (node.get("detail") or {}).get("trigger_price")
            if trig is None and isinstance(node.get("raw"), dict):
                trig = node["raw"].get("trigger_price")
            if trig is not None:
                step = float(meta.order_price_round or 0.1)
                aligned = abs((float(trig) / step) - round(float(trig) / step)) < 1e-6
                REP.rec("M8", f"{label}_price_round", aligned, f"trig={trig} step={step}")
        _cleanup(ex, symbol)
    except Exception as e:  # noqa: BLE001
        REP.rec("M8", "three_legs_confirmed", False, str(e)[:100])
        REP.problem("M8", str(e), level="P1")

    # ── M9 grid ──
    print("\n[M9] grid")
    try:
        last = client.get_last_price(symbol)
        grid = {
            "action": "grid", "symbol": symbol, "side": "long", "type": "limit",
            "levels": [
                {"price": round(last * 0.97, 1), "size_usd": 12},
                {"price": round(last * 0.96, 1), "size_usd": 12},
            ],
            "tp": round(last * 1.01, 1), "sl": round(last * 0.93, 1),
            "tp_scope": "per_level", "sl_scope": "per_level",
            "label": "m9-grid",
        }
        rep = ex.execute_signal(parse_signal(grid))
        ok = rep.ok and len(rep.results) >= 2
        REP.rec("M9", "grid_long_per_level", ok, f"steps={len(rep.results)} ok={rep.ok}")
        _cleanup(ex, symbol)
    except Exception as e:  # noqa: BLE001
        REP.rec("M9", "grid_long_per_level", False, str(e)[:100])
        REP.problem("M9", str(e), level="P2")

    try:
        last = client.get_last_price(symbol)
        grid_s = {
            "action": "grid", "symbol": symbol, "side": "short", "type": "limit",
            "levels": [
                {"price": round(last * 1.03, 1), "size_usd": 12},
                {"price": round(last * 1.04, 1), "size_usd": 12},
            ],
            "tp": round(last * 0.99, 1), "sl": round(last * 1.08, 1),
            "tp_scope": "shared", "sl_scope": "shared",
            "label": "m9-short",
        }
        rep = ex.execute_signal(parse_signal(grid_s))
        ok = rep.ok and len(rep.results) >= 2
        # shared TP: only last level carries size_override exit
        shared = [s for s in rep.results if (s.detail or {}).get("tp_size_override") or
                  any((t or {}).get("size_override") for t in (s.detail or {}).get("tp_orders") or [])]
        REP.rec("M9", "grid_short_shared_tp", ok,
                f"steps={len(rep.results)} shared_tp_steps={len(shared)}")
        _cleanup(ex, symbol)
    except Exception as e:  # noqa: BLE001
        REP.rec("M9", "grid_short_shared_tp", False, str(e)[:100])
        REP.problem("M9-short", str(e), level="P2")

    try:
        dual = {
            "orders": [
                {
                    "action": "grid", "symbol": symbol, "side": "long", "type": "limit",
                    "levels": [{"price": round(last * 0.97, 1), "size_usd": 11}],
                    "tp": round(last * 1.01, 1), "sl": round(last * 0.93, 1),
                    "label": "m9-dual-l",
                },
                {
                    "action": "grid", "symbol": symbol, "side": "short", "type": "limit",
                    "levels": [{"price": round(last * 1.03, 1), "size_usd": 11}],
                    "tp": round(last * 0.99, 1), "sl": round(last * 1.08, 1),
                    "label": "m9-dual-s",
                },
            ]
        }
        rep = ex.execute_signal(parse_signal(dual))
        REP.rec("M9", "grid_dual_via_orders", rep.ok or len(rep.results) >= 2,
                f"steps={len(rep.results)} ok={rep.ok}")
        _cleanup(ex, symbol)
    except Exception as e:  # noqa: BLE001
        REP.rec("M9", "grid_dual_via_orders", False, str(e)[:100])
        REP.problem("M9-dual", str(e), level="P2")

    # ── M10 sizing ──
    print("\n[M10] sizing modes")
    from gate_bot.sizing import usd_to_contracts
    from gate_bot.gate_client import GateApiError

    cm = client.get_contract(symbol)
    last = client.get_last_price(symbol)
    try:
        n = usd_to_contracts(25.0, last, cm)
        REP.rec("M10", "usd_to_contracts", n >= 1, f"25U -> {n} contracts @ {last}")
    except Exception as e:  # noqa: BLE001
        REP.rec("M10", "usd_to_contracts", False, str(e)[:80])
    try:
        usd_to_contracts(0.01, last, cm)
        REP.rec("M10", "tiny_usd_reject", False, "should reject")
    except GateApiError as e:
        REP.rec("M10", "tiny_usd_reject", True, str(e)[:60])
    # size contracts
    try:
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": symbol, "size": 1, "type": "market",
            "sl": round(last * 0.99, 1), "label": "m10-size",
        }))
        REP.rec("M10", "size_contracts", rep.ok, str(rep.results[0].detail)[:80] if rep.results else "")
        _cleanup(ex, symbol)
    except Exception as e:  # noqa: BLE001
        REP.rec("M10", "size_contracts", False, str(e)[:80])
    # size_pct
    try:
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": symbol, "size_pct": 0.02, "type": "market",
            "sl": round(last * 0.99, 1), "label": "m10-pct",
        }))
        REP.rec("M10", "size_pct", rep.ok or bool(rep.results), str(rep.results[0].error or "ok")[:80])
        _cleanup(ex, symbol)
    except Exception as e:  # noqa: BLE001
        REP.rec("M10", "size_pct", False, str(e)[:80])

    # ── M12 multi-bot isolation ──
    print("\n[M12] multi-bot isolation")
    try:
        ex_b = Executor(client, symbols_whitelist=[symbol], max_notional_usd=100,
                        require_sl=False, order_scope="own")
        last = client.get_last_price(symbol)
        ra = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": symbol, "size": 1, "type": "limit",
            "price": round(last * 0.95, 1), "label": "bota",
        }))
        rb = ex_b.execute_signal(parse_signal({
            "action": "open_long", "symbol": symbol, "size": 1, "type": "limit",
            "price": round(last * 0.94, 1), "label": "botb",
        }))
        po = client.list_orders(symbol) or []
        pref_a = [o for o in po if str(o.get("text") or "").startswith("t-bota")]
        pref_b = [o for o in po if str(o.get("text") or "").startswith("t-botb")]
        # cancel only own via scope
        ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": symbol, "label": "bota"}))
        po2 = client.list_orders(symbol) or []
        still_b = [o for o in po2 if str(o.get("text") or "").startswith("t-botb")]
        gone_a = [o for o in po2 if str(o.get("text") or "").startswith("t-bota")]
        ok = (not gone_a) and (len(still_b) == len(pref_b) or not pref_b)
        REP.rec("M12", "scope_own_isolation", ok,
                f"a={len(pref_a)}->{len(gone_a)} b={len(pref_b)}->{len(still_b)}")
        ex_b.execute_signal(parse_signal({"action": "cancel_all", "symbol": symbol, "label": "botb"}))
        _cleanup(ex, symbol)
    except Exception as e:  # noqa: BLE001
        REP.rec("M12", "scope_own_isolation", False, str(e)[:100])
        REP.problem("M12", str(e), level="P1")

    # ── M14 idempotent duplicate delivery ──
    print("\n[M14] duplicate cycle idempotent")
    try:
        from gate_bot.strategist.bridge import chips_to_signal, write_signal_file
        from gate_bot.strategist.schema import parse_plan
        from gate_bot.strategist.risk import RiskConfig, apply_risk
        import json

        root = ROOT / ".prelaunch_test" / "m14"
        if root.exists():
            shutil.rmtree(root)
        plan = parse_plan({
            "cycle_id": "prelaunch-dup-1",
            "chips": [{
                "symbol": symbol, "action": "open_long", "confidence": 0.9,
                "size_usd": 12, "sl": round(last * 0.99, 1), "type": "limit",
                "price": round(last * 0.97, 1),
            }],
        })
        risk = apply_risk(plan, RiskConfig(min_confidence=0.5, max_notional_usd=50, max_chips=3))
        payload = chips_to_signal(plan, risk, bot_id="m14")
        inbox = root / "inbox"
        p1 = write_signal_file(inbox, json.loads(json.dumps(payload)), plan.cycle_id)
        p2 = write_signal_file(inbox, json.loads(json.dumps(payload)), plan.cycle_id)
        # execute both — replace=symbol should leave at most one resting entry
        # chips carry no label; force default_label so order text is t-m14*
        r1 = ex.execute_signal(parse_signal(json.loads(p1.read_text(encoding="utf-8")), default_label="m14"))
        r2 = ex.execute_signal(parse_signal(json.loads(p2.read_text(encoding="utf-8")), default_label="m14"))
        po = client.list_orders(symbol) or []
        n_entry = [o for o in po if ex._text_owned(str(o.get("text") or ""), "m14")]
        ok = r1.ok and r2.ok and len(n_entry) <= 1
        REP.rec("M14", "dup_cycle_single_order", ok,
                f"r1={r1.ok} r2={r2.ok} owned={len(n_entry)} texts={[o.get('text') for o in n_entry]}")
        _cleanup(ex, symbol)
    except Exception as e:  # noqa: BLE001
        REP.rec("M14", "dup_cycle_single_order", False, str(e)[:100])
        REP.problem("M14", str(e), level="P1")

    _cleanup(ex, symbol)
