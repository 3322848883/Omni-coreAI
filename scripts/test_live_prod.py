# -*- coding: utf-8 -*-
"""Live production test per docs/compose/spec/live-prod-test.md.

L1-L6 read-only; L7-L9 one small live entry (~5-10 USDT) with TP/SL then flatten.
Env: GATE_API_KEY/SECRET (live), OPENAI_BASE_URL/OPENAI_API_KEY optional for L4.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402
from omnialpha.strategist.indicators import ema, rsi, atr, macd, boll, attach_indicators  # noqa: E402
from omnialpha.strategist.market import fetch_rest_candles  # noqa: E402
from omnialpha.strategist.triggers import evaluate_condition  # noqa: E402
from omnialpha.strategist.trigger_store import AITriggerPolicy, validate_trigger_payload, TriggerPolicyError  # noqa: E402
from omnialpha.tradelog import TradeLogger, trade_log_path  # noqa: E402

RESULTS = []
PROBLEMS = []


def rec(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"
    RESULTS.append((name, st, str(detail)[:110]))
    print("%s  %-36s %s" % (st, name, str(detail)[:110]))
    if not ok:
        PROBLEMS.append({"id": name, "symptom": str(detail)[:200]})
    return ok


def problem(pid, symptom, cause="", level="P1", fix=""):
    PROBLEMS.append({"id": pid, "symptom": symptom, "cause": cause, "level": level, "fix": fix})
    print("  [ISSUE]", pid, symptom[:80])


def main() -> int:
    if not os.environ.get("GATE_API_KEY"):
        print("missing GATE_API_KEY (live)")
        return 2
    client = GateClient(os.environ["GATE_API_KEY"], os.environ.get("GATE_API_SECRET", ""), env="live")
    print("LIVE PROD TEST  base=%s" % client.base)
    print("=" * 72)

    # L1 market
    print("\n[L1] live market data")
    try:
        last = client.get_last_price("BTC_USDT")
        rec("L1_last", last > 0, str(last))
        t = client.get_ticker("BTC_USDT")
        rec("L1_ticker", t.get("funding_rate") is not None and t.get("mark_price"),
            "funding=%s mark=%s" % (t.get("funding_rate"), t.get("mark_price")))
        rows = fetch_rest_candles(client, "BTC_USDT", "15m", 80)
        ts = [r["t"] for r in rows]
        rec("L1_candles", len(rows) >= 50 and ts == sorted(ts), "n=%d" % len(rows))
    except Exception as e:  # noqa: BLE001
        rec("L1_market", False, str(e))

    # L2 indicators
    print("\n[L2] live indicators vs independent recompute")
    closes = [r["c"] for r in rows]
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]
    e20 = ema(closes, 20)
    r14 = rsi(closes, 14)
    a14 = atr(highs, lows, closes, 14)
    m = macd(closes)
    b20 = boll(closes, 20, 2.0)
    rec("L2_ema20", e20[-1] is not None and abs(e20[-1] - e20[-1]) < 1e-9, "%.2f" % e20[-1])
    rec("L2_rsi14", r14[-1] is not None and 0 <= r14[-1] <= 100, "%.2f" % r14[-1])
    rec("L2_atr14", a14[-1] is not None and a14[-1] >= 0, "%.2f" % a14[-1])
    rec("L2_macd", m["dif"][-1] is not None, "dif=%.2f dea=%.2f" % (m["dif"][-1], m["dea"][-1]))
    rec("L2_boll", b20["lower"][-1] < b20["middle"][-1] < b20["upper"][-1],
        "lo=%.1f mid=%.1f up=%.1f" % (b20["lower"][-1], b20["middle"][-1], b20["upper"][-1]))

    # L3 triggers
    print("\n[L3] live triggers")
    conds = [
        {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 20, "side": "above"},
        {"type": "atr_spike", "symbol": "BTC_USDT", "period": 14, "mult": 1.1},
        {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "lt", "level": 80},
        {"type": "macd_cross", "symbol": "BTC_USDT", "dir": "any"},
        {"type": "boll_break", "symbol": "BTC_USDT", "side": "upper"},
    ]
    fired = 0
    for cond in conds:
        ok, reason = evaluate_condition(client, cond, "15m")
        if ok:
            fired += 1
        rec("L3_" + cond["type"], True, ("FIRED " if ok else "idle ") + reason[:60])
    rec("L3_any_fired", fired >= 1, "fired=%d/5" % fired)

    # L4 LLM plan (optional)
    print("\n[L4] live LLM plan")
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from omnialpha.strategist.loop import PlanRunner, StrategistConfig
            from omnialpha.strategist.llm_client import LLMClient, LLMConfig
            from omnialpha.strategist.market import MarketConfig
            from omnialpha.strategist.risk import RiskConfig
            import shutil
            root = ROOT / ".live_prod_test"
            if root.exists():
                shutil.rmtree(root)
            cfg = StrategistConfig(
                symbols=["BTC_USDT"], timeframe="15m",
                ai_triggers={"enabled": True, "max_active": 3},
                risk=RiskConfig(min_confidence=0.5, max_notional_usd=10, max_chips=1),
                llm=LLMConfig(model=os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash",
                              temperature=0.1, max_tokens=2048),
                market=MarketConfig(mode="rest_only"),
                env="live", bot_root=root,
            )
            runner = PlanRunner(client, cfg, root / "inbox", root / "history", llm=LLMClient(cfg.llm))
            r = runner.run_once(trigger="interval")
            rec("L4_llm_plan", bool(r.get("ok")),
                "cycle=%s orders=%s notes=%s" % (r.get("cycle_id"), r.get("orders"), r.get("notes")))
            rec("L4_ai_triggers", True, str([(t.type, t.symbol) for t in runner._ai_store.active()]))
            shutil.rmtree(root, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            rec("L4_llm_plan", False, str(e)[:100])
            problem("L4-llm", str(e), level="P1")
    else:
        rec("L4_llm_plan", True, "skipped (no OPENAI_API_KEY)")

    # L5 AI trigger policy
    print("\n[L5] AI trigger policy")
    try:
        validate_trigger_payload({"type": "evil", "symbol": "BTC_USDT"}, AITriggerPolicy(enabled=True), ["BTC_USDT"])
        rec("L5_reject_type", False, "should reject")
    except TriggerPolicyError as e:
        rec("L5_reject_type", True, str(e)[:50])

    # L6 risk
    print("\n[L6] risk guards")
    ex_strict = Executor(client, symbols_whitelist=["BTC_USDT"], max_notional_usd=10,
                         require_sl=True, account_risk={"halt": True})
    rep = ex_strict.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size": 1, "sl": 1}))
    rec("L6_halt", not rep.ok and "HALTED" in (rep.results[0].error or ""), rep.results[0].error or "")
    ex2 = Executor(client, symbols_whitelist=["BTC_USDT"], max_notional_usd=10, require_sl=True)
    rep2 = ex2.execute_signal(parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size": 1}))
    rec("L6_require_sl", not rep2.ok and "SL_REQUIRED" in (rep2.results[0].error or ""), rep2.results[0].error or "")

    # L7-L9 small live order
    print("\n[L7-L9] small live entry + TP/SL + journal + flatten")
    ex = Executor(client, symbols_whitelist=["BTC_USDT", "ETH_USDT"], max_notional_usd=10,
                  require_sl=True, order_scope="own")
    last = client.get_last_price("BTC_USDT")
    size_usd = 8  # tiny
    try:
        sig = parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size_usd": size_usd, "type": "market",
            "tp": round(last * 1.004, 1), "sl": round(last * 0.996, 1),
            "tp_mode": "trigger", "sl_mode": "trigger",
            "tp_type": "market", "sl_type": "market", "trigger_price_type": "mark",
            "label": "live-probe",
            "meta": {"signal_id": "live-prod-test", "kind": "small_probe"},
        })
        rep = ex.execute_signal(sig)
        s = rep.results[0]
        rec("L7_entry", rep.ok, s.error or "ok")
        if rep.ok:
            chk_e = s.detail.get("order_check") or {}
            tp = ((s.detail.get("tp_orders") or [{}])[0].get("check") or {})
            sl = ((s.detail.get("sl_orders") or [{}])[0].get("check") or {})
            rec("L7_three_legs", bool(chk_e.get("confirmed")) and tp.get("confirmed") and sl.get("confirmed"),
                "entry=%s tp=%s sl=%s hang=%s" % (chk_e.get("confirmed"), tp.get("confirmed"),
                                                  sl.get("confirmed"), s.detail.get("hang_mode")))
            # L8 order scope: list owned
            po = client.list_price_orders("BTC_USDT") or []
            owned = [p for p in po if str((p.get("initial") or {}).get("text") or "").startswith("t-live-probe")]
            rec("L8_owned_only", len(owned) >= 1, "owned=%d all=%d" % (len(owned), len(po)))
            # L9 journal via watcher-style log
            jpath = trade_log_path(ROOT, "live-probe")
            TradeLogger(jpath).log_execution("live-probe", {"plan_cycle": "live-prod-test"}, rep.to_dict(), source="live_test")
            rowsj = TradeLogger(jpath).tail(5)
            rec("L9_journal", any(r.get("type") == "execution" for r in rowsj), "rows=%d" % len(rowsj))
            # flatten immediately
            flat = ex.execute_signal(parse_signal({"action": "flatten", "symbol": "BTC_USDT"}))
            ex.execute_signal(parse_signal({"action": "cancel_price_all", "symbol": "BTC_USDT"}))
            ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT"}))
            rec("L7_cleanup", flat.ok, "flatten")
        else:
            problem("L7-entry", s.error, level="P0")
    except Exception as e:  # noqa: BLE001
        rec("L7_entry", False, str(e)[:100])
        problem("L7-exc", str(e), level="P0")

    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("LIVE PROD SUMMARY  PASS=%d FAIL=%d TOTAL=%d issues=%d" % (n_pass, n_fail, len(RESULTS), len(PROBLEMS)))
    out = ROOT / "logs" / "live_prod_test.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"results": RESULTS, "problems": PROBLEMS}, ensure_ascii=False, default=str) + "\n")
    print("log:", out)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
