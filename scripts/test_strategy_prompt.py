# -*- coding: utf-8 -*-
"""Strategy prompt + LLM plan end-to-end test (OpenAI-compatible endpoint).

Env required (never hardcode keys):
  OPENAI_BASE_URL  e.g. http://host:port/v1
  OPENAI_API_KEY   bearer token
Optional:
  LLM_MODEL        default global:deepseek-v4.1-flash
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.gate_client import GateClient  # noqa: E402
from gate_bot.strategist.llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402
from gate_bot.strategist.market import MarketConfig  # noqa: E402
from gate_bot.strategist.prompt import (  # noqa: E402
    PLAN_SCHEMA_HINT,
    SYSTEM_PROMPT,
    build_system_prompt,
    build_user_prompt,
    load_strategy_prompt,
)
from gate_bot.strategist.risk import RiskConfig, apply_risk  # noqa: E402
from gate_bot.strategist.schema import PlanError, parse_plan_text  # noqa: E402
from gate_bot.strategist.snapshot import collect_snapshot  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    RESULTS.append((name, status, str(detail)[:120]))
    print("%s  %-32s %s" % (status, name, str(detail)[:120]))
    return ok


def main() -> int:
    base = os.environ.get("OPENAI_BASE_URL") or ""
    key = os.environ.get("OPENAI_API_KEY") or ""
    model = os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash"
    if not base or not key:
        print("missing OPENAI_BASE_URL / OPENAI_API_KEY")
        return 2

    # ── 1 prompt assembly ─────────────────────────────────
    print("\n[1] prompt assembly")
    strategy = load_strategy_prompt(ROOT / "prompts" / "vergex_default.md")
    system = build_system_prompt(strategy)
    rec("system_prompt", "chips" in system and "策略人格" in system and "hold" in system,
        "len=%d" % len(system))
    rec("schema_hint", "open_long" in PLAN_SCHEMA_HINT and "stop_entry" in PLAN_SCHEMA_HINT,
        PLAN_SCHEMA_HINT[:80])

    # ── 2 live snapshot (testnet) ─────────────────────────
    print("\n[2] snapshot (testnet REST)")
    env = "testnet"
    gate_key = os.environ.get("GATE_TESTNET_API_KEY") or ""
    gate_sec = os.environ.get("GATE_TESTNET_API_SECRET") or ""
    client = GateClient(gate_key, gate_sec, env=env)
    symbols = ["BTC_USDT", "ETH_USDT"]
    snapshot = collect_snapshot(
        client,
        symbols,
        candles=40,
        interval="15m",
        market_cfg=MarketConfig(mode="rest_only", refresh=["ticker", "stats", "orderbook"]),
        env=env,
    )
    rec("snapshot_ok", bool(snapshot.get("market")) and "account" in snapshot,
        "symbols=%s degraded=%s" % (list(snapshot["market"]), snapshot["meta"].get("degraded")))
    user = build_user_prompt(
        snapshot,
        {"min_confidence": 0.7, "max_notional_usd": 30, "max_chips": 2,
         "allow_actions": ["hold", "open_long", "open_short", "close", "reduce_long", "reduce_short"]},
        symbols,
    )
    rec("user_prompt", "市场与账户快照" in user and "BTC_USDT" in user, "len=%d" % len(user))
    print("    system_chars", len(system), "user_chars", len(user))

    # ── 3 LLM chat ────────────────────────────────────────
    print("\n[3] LLM chat model=%s" % model)
    llm = LLMClient(LLMConfig(model=model, temperature=0.1, timeout_sec=90, max_tokens=4096))
    t0 = time.time()
    try:
        text = llm.chat(system, user)
        rec("llm_chat", bool(text and text.strip()),
            "latency=%.1fs chars=%d finish=%s usage=%s" % (
                time.time() - t0, len(text),
                getattr(llm, "last_finish_reason", None),
                (getattr(llm, "last_usage", {}) or {}).get("completion_tokens"),
            ))
    except LLMError as e:
        rec("llm_chat", False, str(e))
        return 1
    print("    raw_head:", (text or "")[:240].replace("\n", " "))

    # ── 4 parse Plan JSON ─────────────────────────────────
    print("\n[4] parse plan")
    try:
        plan = parse_plan_text(text)
        rec("parse_plan", True, "cycle=%s chips=%d" % (plan.cycle_id, len(plan.chips)))
    except PlanError as e:
        rec("parse_plan", False, str(e))
        return 1

    for c in plan.chips:
        print("    chip", c.symbol, c.action, "conf=%.2f" % c.confidence,
              "size_usd=", c.size_usd, "tp=", c.tp, "sl=", c.sl, "|", (c.reasoning or "")[:60])

    # ── 5 risk gate ───────────────────────────────────────
    print("\n[5] risk gate")
    risk = apply_risk(plan, RiskConfig(
        min_confidence=0.7, max_notional_usd=30, max_chips=2,
        allow_actions={"hold", "open_long", "open_short", "close", "reduce_long", "reduce_short"},
    ))
    rec("risk_ok", True, "accepted=%d rejected=%d notes=%s" % (
        len(risk.accepted), len(risk.rejected), (risk.notes or [])[:3]))
    for c in risk.accepted:
        print("    accept", c.symbol, c.action, c.size_usd)
    for c in risk.rejected:
        print("    reject", c.symbol, c.action)

    # ── 6 hold / no-hold behaviour ─────────────────────────
    acts = {c.action for c in plan.chips}
    rec("actions_in_allowlist", acts <= {
        "hold", "open_long", "open_short", "add_long", "add_short",
        "reduce_long", "reduce_short", "close", "close_all",
        "stop_entry_long", "stop_entry_short", "flatten", "cancel_all", "cancel_price_all",
    }, str(sorted(acts)))

    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("SUMMARY  PASS=%d  FAIL=%d  TOTAL=%d" % (n_pass, n_fail, len(RESULTS)))
    out = ROOT / "logs" / "strategy_prompt_test.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "model": model,
            "results": RESULTS,
            "plan": {
                "cycle_id": plan.cycle_id,
                "reasoning": plan.reasoning,
                "chips": [c.to_signal_dict() for c in plan.chips],
            },
        }, ensure_ascii=False) + "\n")
    print("log:", out)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
