# -*- coding: utf-8 -*-
"""Full chain: market snapshot → LLM plan → risk → inbox → executor → Gate exchange.

Env:
  OPENAI_BASE_URL / OPENAI_API_KEY  (LLM)
  GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET  (testnet execution)
Optional: LLM_MODEL (default global:deepseek-v4.1-flash)

If the LLM only holds (safe default), a tiny bridge-path chip is injected through
the SAME chips_to_signal → write_signal_file → process_file path so the
strategy→exchange hop is still proven on testnet.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.config import BotConfig  # noqa: E402
from gate_bot.gate_client import GateApiError, GateClient  # noqa: E402
from gate_bot.strategist.bridge import chips_to_signal, write_hold_audit, write_signal_file  # noqa: E402
from gate_bot.strategist.llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402
from gate_bot.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402
from gate_bot.strategist.market import MarketConfig  # noqa: E402
from gate_bot.strategist.prompt import build_system_prompt, build_user_prompt, load_strategy_prompt  # noqa: E402
from gate_bot.strategist.risk import RiskConfig, apply_risk  # noqa: E402
from gate_bot.strategist.schema import Plan, Chip, parse_plan_text  # noqa: E402
from gate_bot.strategist.snapshot import collect_snapshot  # noqa: E402
from gate_bot.watcher import ProjectPaths, process_file  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    RESULTS.append((name, status, str(detail)[:130]))
    print("%s  %-34s %s" % (status, name, str(detail)[:130]))
    return ok


def main() -> int:
    env = "testnet"
    llm_model = os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash"
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_BASE_URL"):
        print("missing OPENAI_BASE_URL / OPENAI_API_KEY")
        return 2
    if not os.environ.get("GATE_TESTNET_API_KEY"):
        print("missing GATE_TESTNET_API_KEY")
        return 2

    root = ROOT / ".full_chain_test"
    if root.exists():
        shutil.rmtree(root)
    paths = ProjectPaths(root)
    paths.ensure()
    bot_id = "chain"
    inbox = paths.bot_inbox(bot_id)
    history = paths.root / "history" / bot_id

    client = GateClient(
        os.environ["GATE_TESTNET_API_KEY"],
        os.environ["GATE_TESTNET_API_SECRET"],
        env=env,
    )
    symbols = ["BTC_USDT"]
    print("CHAIN testnet  model=%s  root=%s" % (llm_model, root))
    print("=" * 72)

    # ── hop 1: market snapshot ─────────────────────────────
    print("\n[h1] market snapshot")
    snapshot = collect_snapshot(
        client, symbols, candles=40, interval="15m",
        market_cfg=MarketConfig(mode="rest_only", refresh=["ticker", "stats", "orderbook"]),
        env=env,
    )
    rec("h1_snapshot", "last" in snapshot["market"]["BTC_USDT"] and "account" in snapshot,
        "last=%s funding=%s" % (
            snapshot["market"]["BTC_USDT"].get("last"),
            (snapshot["market"]["BTC_USDT"].get("ticker") or {}).get("funding_rate"),
        ))

    # ── hop 2: prompt + LLM plan ───────────────────────────
    print("\n[h2] prompt + LLM plan")
    strategy = load_strategy_prompt(ROOT / "prompts" / "vergex_default.md")
    system = build_system_prompt(strategy)
    user = build_user_prompt(
        snapshot,
        {"min_confidence": 0.7, "max_notional_usd": 25, "max_chips": 1,
         "allow_actions": ["hold", "open_long", "open_short", "close"]},
        symbols,
    )
    llm = LLMClient(LLMConfig(model=llm_model, temperature=0.1, timeout_sec=90, max_tokens=4096))
    try:
        text = llm.chat(system, user)
        rec("h2_llm", bool(text), "finish=%s chars=%d" % (
            getattr(llm, "last_finish_reason", None), len(text)))
    except LLMError as e:
        rec("h2_llm", False, str(e))
        return 1

    # ── hop 3: parse plan ──────────────────────────────────
    print("\n[h3] parse Plan")
    try:
        plan = parse_plan_text(text)
        rec("h3_parse", True, "cycle=%s chips=%d reasoning=%s" % (
            plan.cycle_id, len(plan.chips), (plan.reasoning or "")[:40]))
    except Exception as e:  # noqa: BLE001
        rec("h3_parse", False, str(e))
        return 1

    # ── hop 4: risk ────────────────────────────────────────
    print("\n[h4] risk gate")
    risk_cfg = RiskConfig(
        min_confidence=0.7, max_notional_usd=25, max_chips=1,
        allow_actions={"hold", "open_long", "open_short", "close"},
    )
    risk = apply_risk(plan, risk_cfg)
    tradeable = [c for c in risk.accepted if c.action != "hold"]
    rec("h4_risk", True, "accepted=%d tradeable=%d rejected=%d" % (
        len(risk.accepted), len(tradeable), len(risk.rejected)))

    injected = False
    if not tradeable:
        # Same bridge path with a tiny plan chip to prove strategy→exchange hop.
        print("    LLM is hold-only (safe); injecting tiny bridge chip for exec hop")
        forced = Plan(
            cycle_id="chain-force-%d" % int(time.time()),
            reasoning="chain-test tiny probe",
            chips=[Chip(
                symbol="BTC_USDT", action="open_long", confidence=0.9,
                size_usd=20, order_type="limit",
                price=round(client.get_last_price("BTC_USDT") * 0.97, 1),
                sl=round(client.get_last_price("BTC_USDT") * 0.95, 1),
                tp=round(client.get_last_price("BTC_USDT") * 1.05, 1),
                reasoning="chain-test",
            )],
        )
        risk = apply_risk(forced, risk_cfg)
        plan = forced
        tradeable = [c for c in risk.accepted if c.action != "hold"]
        injected = True
        rec("h4b_inject_probe", len(tradeable) == 1, "action=%s size=%s" % (
            tradeable[0].action if tradeable else None,
            tradeable[0].size_usd if tradeable else None))

    # ── hop 5: bridge → inbox ──────────────────────────────
    print("\n[h5] bridge → inbox")
    payload = chips_to_signal(plan, risk, bot_id=bot_id)
    if not payload.get("orders"):
        write_hold_audit(history, plan, cycle_id=plan.cycle_id)
        rec("h5_inbox", False, "no orders after risk (hold-only)")
        return 1
    sig_path = write_signal_file(inbox, payload, cycle_id=plan.cycle_id)
    inbox_files = list(inbox.glob("*.json"))
    rec("h5_inbox", sig_path.exists() and len(inbox_files) == 1,
        "file=%s orders=%d injected=%s" % (sig_path.name, len(payload["orders"]), injected))

    # ── hop 6: watcher → executor ──────────────────────────
    print("\n[h6] watcher process_file → executor")
    bot = BotConfig(
        bot_id=bot_id, env=env, symbols=["BTC_USDT", "ETH_USDT"],
        max_notional_usd=25, max_orders_per_file=5,
        api_key_env="GATE_TESTNET_API_KEY",
        api_secret_env="GATE_TESTNET_API_SECRET",
        position_policy="free", default_replace="symbol",
        label_prefix="chain",
    )
    ok = process_file(sig_path, bot, paths, executor=None)
    done = list(paths.bot_done(bot_id).glob("*.json"))
    failed = [p for p in paths.bot_failed(bot_id).glob("*.json") if not p.name.endswith(".error.json")]
    rec("h6_execute", bool(ok), "process_file=%s done=%d failed=%d" % (ok, len(done), len(failed)))
    if not ok and failed:
        err = failed[0].with_suffix("").with_suffix("")
        ep = paths.bot_failed(bot_id) / (failed[0].stem + ".error.json")
        if ep.exists():
            print("    error:", ep.read_text(encoding="utf-8")[:200])

    # ── hop 7: exchange state ──────────────────────────────
    print("\n[h7] exchange state")
    try:
        orders = client.list_orders("BTC_USDT") or []
        pos = [p for p in (client.get_positions() or []) if int(p.get("size") or 0) != 0]
        rec("h7_exchange", True, "open_orders=%d open_pos=%d sample_order=%s" % (
            len(orders), len(pos), (orders[0].get("id") if orders else None)))
    except GateApiError as e:
        rec("h7_exchange", False, str(e))

    # ── hop 8: trade journal ───────────────────────────────
    print("\n[h8] trade journal")
    from gate_bot.tradelog import TradeLogger, trade_log_path
    rows = TradeLogger(trade_log_path(paths.root, bot_id)).tail(5)
    rec("h8_tradelog", any(r.get("type") == "execution" for r in rows),
        "rows=%d types=%s" % (len(rows), [r.get("type") for r in rows]))

    # ── hop 9: cleanup flatten ─────────────────────────────
    print("\n[h9] cleanup")
    from gate_bot.executor import Executor
    from gate_bot.schema import parse_signal
    ex = Executor(client, symbols_whitelist=["BTC_USDT"], max_notional_usd=25)
    flat = ex.execute_signal(parse_signal({"action": "flatten", "symbol": "BTC_USDT", "label": "chain-fin"}))
    cancel = ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT"}))
    rec("h9_cleanup", flat.ok and cancel.ok, "flatten=%s cancel=%s" % (flat.ok, cancel.ok))

    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("CHAIN SUMMARY  PASS=%d  FAIL=%d  TOTAL=%d  (injected_probe=%s)" % (
        n_pass, n_fail, len(RESULTS), injected))
    out = ROOT / "logs" / "full_chain_test.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"results": RESULTS, "injected": injected, "model": llm_model},
                           ensure_ascii=False) + "\n")
    print("log:", out)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(2)
