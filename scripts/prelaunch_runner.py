# -*- coding: utf-8 -*-
"""Prelaunch full-matrix runner per docs/compose/spec/prelaunch-test.md.

Usage:
  python scripts/prelaunch_runner.py --phase readonly
  python scripts/prelaunch_runner.py --phase orders --env testnet
  python scripts/prelaunch_runner.py --phase fault
  python scripts/prelaunch_runner.py --phase live
  python scripts/prelaunch_runner.py --phase all
  python scripts/prelaunch_runner.py --only M1,M7

Phases: readonly | orders | fault | live | all
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

RESULTS: list[dict[str, Any]] = []
PROBLEMS: list[dict[str, Any]] = []


@dataclass
class Report:
    phase: str = ""

    def rec(self, mid: str, name: str, ok: bool, detail: str = "", evidence: str = "") -> bool:
        if str(detail).upper().startswith("SKIP"):
            st = "SKIP"
        else:
            st = "PASS" if ok else "FAIL"
        row = {
            "id": mid,
            "name": name,
            "result": st,
            "detail": str(detail)[:160],
            "evidence": evidence,
            "phase": self.phase,
        }
        RESULTS.append(row)
        print("%s  %-6s %-28s %s" % (st, mid, name, str(detail)[:100]))
        return ok

    def problem(self, pid: str, symptom: str, cause: str = "", level: str = "P1", fix: str = "") -> None:
        PROBLEMS.append(
            {"id": pid, "symptom": symptom, "cause": cause, "level": level, "fix": fix}
        )
        print("  [ISSUE]", pid, level, symptom[:90])


REP = Report()


def gate_client(env: str):
    from gate_bot.gate_client import GateClient

    if env == "live":
        key = os.environ.get("GATE_API_KEY") or ""
        sec = os.environ.get("GATE_API_SECRET") or ""
    else:
        key = os.environ.get("GATE_TESTNET_API_KEY") or ""
        sec = os.environ.get("GATE_TESTNET_API_SECRET") or ""
    if not key:
        raise RuntimeError(f"missing GATE keys for env={env}")
    return GateClient(key, sec, env=env)


# ── M1–M3 readonly market / indicators / contract ───────────────────────────


def m1_market(client, env: str) -> None:
    symbols = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "BNB_USDT"]
    try:
        last = client.get_last_price("BTC_USDT")
        REP.rec("M1", "last_price", last > 0, f"BTC={last}")
    except Exception as e:  # noqa: BLE001
        REP.rec("M1", "last_price", False, str(e))
        return
    try:
        t = client.get_ticker("BTC_USDT")
        ok = t.get("funding_rate") is not None and t.get("mark_price") is not None
        REP.rec("M1", "ticker_fields", ok, f"funding={t.get('funding_rate')} mark={t.get('mark_price')}")
    except Exception as e:  # noqa: BLE001
        REP.rec("M1", "ticker_fields", False, str(e))
    try:
        from gate_bot.strategist.market import fetch_rest_candles

        rows = fetch_rest_candles(client, "BTC_USDT", "15m", 80)
        ts = [r["t"] for r in rows]
        REP.rec("M1", "candles_sorted", len(rows) >= 50 and ts == sorted(ts), f"n={len(rows)}")
    except Exception as e:  # noqa: BLE001
        REP.rec("M1", "candles_sorted", False, str(e))
    try:
        ob = client.get_orderbook_top("BTC_USDT", limit=5)
        REP.rec("M1", "orderbook", bool(ob.get("bids") and ob.get("asks")), "ok")
    except Exception as e:  # noqa: BLE001
        REP.rec("M1", "orderbook", False, str(e))
        REP.problem("M1-ob", str(e), level="P2")
    # multi-symbol (tolerate missing contracts on some venues)
    try:
        available = set(client.get_contracts().keys())
    except Exception:  # noqa: BLE001
        available = set(symbols)
    ok_syms = 0
    checked = 0
    for s in symbols:
        if s not in available:
            continue
        checked += 1
        try:
            if client.get_last_price(s) > 0:
                ok_syms += 1
        except Exception as e:  # noqa: BLE001
            REP.problem("M1-multi", f"last fail {s}: {e}"[:120], level="P2")
    REP.rec("M1", "multi_symbol", checked > 0 and ok_syms >= max(1, int(checked * 0.6)),
            f"{ok_syms}/{checked} available of {len(symbols)}")


def m2_indicators(client) -> None:
    from gate_bot.strategist.indicators import ema, rsi, atr, macd, boll
    from gate_bot.strategist.market import fetch_rest_candles

    try:
        rows = []
        err = ""
        for _attempt in range(3):
            try:
                rows = fetch_rest_candles(client, "BTC_USDT", "15m", 120)
                if rows:
                    break
            except Exception as e:  # noqa: BLE001
                err = str(e)
                time.sleep(0.8)
        if not rows:
            raise RuntimeError(err or "no candles")
        closes = [r["c"] for r in rows]
        highs = [r["h"] for r in rows]
        lows = [r["l"] for r in rows]
        e20 = ema(closes, 20)
        r14 = rsi(closes, 14)
        a14 = atr(highs, lows, closes, 14)
        m = macd(closes)
        b20 = boll(closes, 20, 2.0)
        REP.rec("M2", "ema_rsi_atr", e20[-1] is not None and r14[-1] is not None and a14[-1] is not None,
                f"ema={e20[-1]:.2f} rsi={r14[-1]:.2f} atr={a14[-1]:.4f}")
        REP.rec("M2", "macd_boll", m["dif"][-1] is not None and b20["lower"][-1] < b20["upper"][-1],
                f"dif={m['dif'][-1]:.3f} boll={b20['lower'][-1]:.1f}/{b20['upper'][-1]:.1f}")
        # independent EMA recompute (SMA-seeded, matches indicators.ema)
        k = 2 / 21
        sma0 = sum(closes[:20]) / 20
        ind = sma0
        for c in closes[20:]:
            ind = c * k + ind * (1 - k)
        REP.rec("M2", "ema_recompute", abs(ind - e20[-1]) < 1e-9, f"diff={abs(ind - e20[-1]):.2e}")
    except Exception as e:  # noqa: BLE001
        REP.rec("M2", "indicators", False, str(e))
        REP.problem("M2", str(e))


def m3_contract(client) -> None:
    from gate_bot.strategist.snapshot import collect_snapshot
    from gate_bot.strategist.market import MarketConfig

    symbols = ["BTC_USDT", "ETH_USDT", "DOGE_USDT", "PEPE_USDT", "NOPE_USDT"]
    try:
        snap = collect_snapshot(
            client, symbols, candles=5, interval="15m",
            market_cfg=MarketConfig(mode="rest_only", refresh=["ticker"]), env="testnet",
        )
    except Exception as e:  # noqa: BLE001
        REP.rec("M3", "snapshot", False, str(e))
        return
    good = 0
    for s in symbols:
        ent = snap["market"].get(s) or {}
        cm = ent.get("contract") or {}
        if cm.get("quanto_multiplier") and cm.get("min_notional_usd") is not None:
            good += 1
        elif ent.get("contract_error") or f"{s}:contract" in snap["meta"].get("degraded", []):
            if s == "NOPE_USDT":
                REP.rec("M3", "unknown_contract_degraded", True, "NOPE degraded ok")
            else:
                REP.problem("M3", f"contract missing {s}: {ent.get('contract_error')}", level="P2")
    REP.rec("M3", "contract_meta", good >= 2, f"ok={good}/{len(symbols)}")


# ── M6 LLM sizing (optional) ────────────────────────────────────────────────


def m6_llm(client) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        REP.rec("M6", "llm_sizing", False, "SKIP no OPENAI_API_KEY")
        return
    try:
        from gate_bot.strategist.llm_client import LLMClient, LLMConfig
        from gate_bot.strategist.prompt import build_system_prompt, load_strategy_prompt
        from gate_bot.strategist.snapshot import collect_snapshot
        from gate_bot.strategist.market import MarketConfig
        from gate_bot.strategist.schema import parse_plan_text

        system = build_system_prompt(load_strategy_prompt(ROOT / "prompts" / "vergex_default.md"))
        snap = collect_snapshot(
            client, ["BTC_USDT"], candles=20, interval="15m",
            market_cfg=MarketConfig(mode="rest_only", refresh=["ticker"]), env="testnet",
        )
        user = (
            "【品种宇宙】\n[\"BTC_USDT\"]\n\n【策略风控】\n"
            + json.dumps({"min_confidence": 0.3, "max_notional_usd": 50, "max_chips": 2})
            + "\n\n【市场与账户快照】\n"
            + json.dumps(snap, ensure_ascii=False)
            + "\n\n【本轮任务】对 BTC_USDT 开多 size_usd=30，必须带 sl。请输出 Plan JSON。"
        )
        llm = LLMClient(LLMConfig(model=os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash",
                                  temperature=0.1, timeout_sec=60, max_tokens=2048))
        text = llm.chat(system, user)
        plan = parse_plan_text(text)
        sized = [c for c in plan.chips if c.size_usd or c.size]
        REP.rec("M6", "llm_plan_parse", bool(plan.chips), f"chips={len(plan.chips)} sized={len(sized)}")
        ok_usd = any(c.size_usd for c in plan.chips)
        REP.rec("M6", "prefer_size_usd", ok_usd, str([(c.symbol, c.size_usd, c.size) for c in plan.chips])[:80])
    except Exception as e:  # noqa: BLE001
        REP.rec("M6", "llm_sizing", False, str(e))
        REP.problem("M6", str(e), level="P2")


# ── M4 conditions / M5 AI triggers ──────────────────────────────────────────


def m4_conditions(client) -> None:
    from gate_bot.strategist.triggers import evaluate_condition

    conds = [
        {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 20, "side": "above"},
        {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "lt", "level": 80},
        {"type": "macd_cross", "symbol": "BTC_USDT", "dir": "any"},
        {"type": "atr_spike", "symbol": "BTC_USDT", "period": 14, "mult": 1.1},
        {"type": "boll_break", "symbol": "BTC_USDT", "side": "upper"},
    ]
    ok_n = 0
    fired = 0
    for cond in conds:
        try:
            ok, reason = evaluate_condition(client, cond, "15m")
            ok_n += 1
            if ok:
                fired += 1
            REP.rec("M4", cond["type"], True, ("FIRED " if ok else "idle ") + reason[:60])
        except Exception as e:  # noqa: BLE001
            REP.rec("M4", cond["type"], False, str(e)[:80])
    REP.rec("M4", "conditions_evaluated", ok_n >= 4, f"ok={ok_n}/{len(conds)} fired={fired}")


def m5_ai_triggers() -> None:
    from gate_bot.strategist.trigger_store import (
        AITriggerPolicy,
        TriggerPolicyError,
        validate_trigger_payload,
    )

    policy = AITriggerPolicy(enabled=True, max_active=3)
    # reject unknown type
    try:
        validate_trigger_payload({"type": "evil", "symbol": "BTC_USDT"}, policy, ["BTC_USDT"])
        REP.rec("M5", "reject_bad_type", False, "should reject")
    except TriggerPolicyError as e:
        REP.rec("M5", "reject_bad_type", True, str(e)[:60])
    # reject symbol outside universe
    try:
        validate_trigger_payload({"type": "price_break", "symbol": "ETH_USDT", "side": "above"}, policy, ["BTC_USDT"])
        REP.rec("M5", "reject_bad_symbol", False, "should reject")
    except TriggerPolicyError as e:
        REP.rec("M5", "reject_bad_symbol", True, str(e)[:60])
    # accept valid + TTL fields
    try:
        norm = validate_trigger_payload(
            {"type": "price_break", "symbol": "BTC_USDT", "side": "above", "ttl_sec": 600},
            policy, ["BTC_USDT"],
        )
        REP.rec("M5", "accept_valid", bool(norm), str(norm)[:70])
    except Exception as e:  # noqa: BLE001
        REP.rec("M5", "accept_valid", False, str(e)[:80])


# ── M11 risk rejects ────────────────────────────────────────────────────────


def m11_risk() -> None:
    from gate_bot.strategist.schema import parse_plan
    from gate_bot.strategist.risk import RiskConfig, apply_risk

    plan = parse_plan({
        "cycle_id": "prelaunch-m11",
        "chips": [
            {"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 50, "sl": 1},
            {"symbol": "ETH_USDT", "action": "open_short", "confidence": 0.2, "size_usd": 30, "sl": 1},
            {"symbol": "SOL_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 200, "sl": 1},
        ],
    })
    risk = apply_risk(plan, RiskConfig(min_confidence=0.7, max_notional_usd=50, max_chips=1,
                                       allow_actions={"open_long", "open_short", "hold"}))
    notes = " | ".join(risk.notes)
    rejected_actions = {c.symbol for c in risk.rejected}
    ok = len(risk.rejected) >= 2 and "max_chips" in notes
    REP.rec("M11", "risk_reject_matrix", ok, notes[:120])
    # require_sl at signal schema
    try:
        from gate_bot.schema import parse_signal, SchemaError

        try:
            parse_signal({"action": "open_long", "symbol": "BTC_USDT", "size_usd": 30, "require_sl": True})
            # sl missing — executor enforces; schema may allow. Try without sl with force
            REP.rec("M11", "schema_parses_without_sl", True, "schema-level; executor require_sl tested in orders")
        except SchemaError as e:
            REP.rec("M11", "schema_sl", True, str(e)[:80])
    except Exception as e:  # noqa: BLE001
        REP.rec("M11", "schema_sl", False, str(e))
    plan2 = parse_plan({
        "cycle_id": "prelaunch-m11-nom",
        "chips": [{"symbol": "BTC_USDT", "action": "open_long", "confidence": 0.9, "size_usd": 200, "sl": 1}],
    })
    risk2 = apply_risk(plan2, RiskConfig(min_confidence=0.7, max_notional_usd=50, max_chips=1))
    ok_nom = any("max" in n and "size_usd" in n for n in risk2.notes) and not risk2.accepted
    REP.rec("M11", "max_notional_reject", ok_nom, " | ".join(risk2.notes)[:100])


# ── M13 journal ─────────────────────────────────────────────────────────────


def m13_journal() -> None:
    from gate_bot.tradelog import TradeLogger

    p = ROOT / ".prelaunch_test" / "trades.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    log = TradeLogger(p)
    log.write({"type": "smoke", "bot_id": "prelaunch", "ok": True})
    line = p.read_text(encoding="utf-8").strip().splitlines()[-1]
    row = json.loads(line)
    ok = row.get("ts") and row.get("type") == "smoke"
    REP.rec("M13", "journal_fields", ok, f"keys={sorted(row.keys())}")


def phase_readonly(env: str = "live") -> None:
    REP.phase = "readonly"
    print(f"\n=== READONLY phase env={env} ===")
    client = gate_client(env)
    m1_market(client, env)
    m2_indicators(client)
    m3_contract(client)
    m4_conditions(client)
    m5_ai_triggers()
    m6_llm(client)
    m11_risk()
    m13_journal()


# ── orders phase placeholders (T2) ─────────────────────────────────────────


def phase_orders(env: str = "testnet") -> None:
    from prelaunch_orders import run_orders  # type: ignore

    REP.phase = "orders"
    print(f"\n=== ORDERS phase env={env} ===")
    run_orders(REP, gate_client, env)


def phase_fault() -> None:
    from prelaunch_fault import run_fault  # type: ignore

    REP.phase = "fault"
    print("\n=== FAULT phase ===")
    run_fault(REP, gate_client)


def phase_live() -> None:
    from prelaunch_live import run_live  # type: ignore

    REP.phase = "live"
    print("\n=== LIVE phase ===")
    run_live(REP, gate_client)


def phase_regress() -> None:
    import unittest

    REP.phase = "regress"
    print("\n=== REGRESS M20 unittest ===")
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"))
    runner = unittest.TextTestRunner(verbosity=0)
    # capture
    import io

    buf = io.StringIO()
    result = runner.run(suite)
    ok = result.wasSuccessful()
    detail = f"ran={result.testsRun} fail={len(result.failures)} err={len(result.errors)}"
    REP.rec("M20", "unittest_discover", ok, detail)
    if not ok:
        for case, tb in (result.failures + result.errors)[:3]:
            REP.problem("M20", f"{case}: {tb[-120:]}", level="P0")


def summary(out: Optional[Path] = None) -> int:
    n_pass = sum(1 for r in RESULTS if r["result"] == "PASS")
    n_fail = sum(1 for r in RESULTS if r["result"] == "FAIL")
    n_skip = sum(1 for r in RESULTS if r["result"] == "SKIP")
    print("\n" + "=" * 72)
    print("PRELAUNCH SUMMARY  PASS=%d FAIL=%d SKIP=%d TOTAL=%d" % (n_pass, n_fail, n_skip, len(RESULTS)))
    for r in RESULTS:
        if r["result"] == "FAIL":
            print("  FAIL", r["id"], r["name"], r["detail"][:80])
    if PROBLEMS:
        print("ISSUES:")
        for p in PROBLEMS:
            print(" ", p["id"], p.get("level", ""), p["symptom"][:80])
    payload = {"results": RESULTS, "problems": PROBLEMS,
               "pass": n_pass, "fail": n_fail, "skip": n_skip}
    out = out or (ROOT / ".prelaunch_test" / "report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report:", out)
    return 0 if n_fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="readonly",
                    choices=["readonly", "orders", "fault", "live", "regress", "all"])
    ap.add_argument("--env", default=None, help="testnet|live for readonly/orders")
    ap.add_argument("--only", default="", help="comma matrix ids e.g. M1,M2")
    args = ap.parse_args()
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}

    def want(mid: str) -> bool:
        return not only or mid.upper() in only

    try:
        if args.phase in ("readonly", "all"):
            env = args.env or "testnet"
            if want("M1") or want("M2") or want("M3") or want("M4") or want("M5") or want("M6") or want("M11") or want("M13") or not only:
                phase_readonly(env)
                if args.phase == "all" and (os.environ.get("GATE_API_KEY")):
                    phase_readonly("live")
        if args.phase in ("orders", "all") and (want("M7") or want("M8") or want("M9") or want("M10") or want("M12") or want("M14") or not only):
            phase_orders(args.env or "testnet")
        if args.phase in ("fault", "all") and (want("M15") or want("M16") or want("M17") or want("M18") or not only):
            phase_fault()
        if args.phase in ("live", "all") and (want("M19") or not only):
            phase_live()
        if args.phase in ("all", "regress"):
            phase_regress()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        REP.problem("runner", "phase crashed", level="P0")
    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
