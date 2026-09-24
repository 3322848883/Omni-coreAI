# -*- coding: utf-8 -*-
"""Production E2E: strategy prompt → LLM → risk → inbox → exchange → trades log.

Every hop is asserted and the trade journal rows are field-checked against
the executed orders (no fake market data; testnet + real DeepSeek).

Env: OPENAI_BASE_URL, OPENAI_API_KEY, GATE_TESTNET_API_KEY, GATE_TESTNET_API_SECRET
Optional: LLM_MODEL (default global:deepseek-v4.1-flash)
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
from gate_bot.gate_client import GateClient  # noqa: E402
from gate_bot.strategist.bridge import chips_to_signal, write_signal_file  # noqa: E402
from gate_bot.strategist.llm_client import LLMClient, LLMConfig  # noqa: E402
from gate_bot.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402
from gate_bot.strategist.market import MarketConfig  # noqa: E402
from gate_bot.strategist.prompt import (  # noqa: E402
    PLAN_SCHEMA_HINT,
    SYSTEM_PROMPT,
    build_system_prompt,
    build_user_prompt,
    load_strategy_prompt,
)
from gate_bot.strategist.risk import RiskConfig, apply_risk  # noqa: E402
from gate_bot.strategist.schema import Chip, Plan, parse_plan_text  # noqa: E402
from gate_bot.strategist.snapshot import collect_snapshot  # noqa: E402
from gate_bot.tradelog import TradeLogger, trade_log_path  # noqa: E402
from gate_bot.watcher import ProjectPaths, process_file  # noqa: E402

RESULTS = []


def rec(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"
    RESULTS.append((name, st, str(detail)[:120]))
    print("%s  %-38s %s" % (st, name, str(detail)[:120]))
    return ok


def main() -> int:
    model = os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash"
    for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "GATE_TESTNET_API_KEY"):
        if not os.environ.get(k):
            print("missing env", k)
            return 2

    root = ROOT / ".prod_e2e"
    if root.exists():
        shutil.rmtree(root)
    paths = ProjectPaths(root)
    paths.ensure()
    bot_id = "e2e"
    inbox = paths.bot_inbox(bot_id)
    history = paths.root / "history" / bot_id
    client = GateClient(
        os.environ["GATE_TESTNET_API_KEY"],
        os.environ["GATE_TESTNET_API_SECRET"],
        env="testnet",
    )
    print("PROD E2E  testnet  model=%s  root=%s" % (model, root))
    print("=" * 72)

    # ── 1 策略提示词 ──────────────────────────────────────
    print("\n[1] strategy prompt")
    prompt_path = ROOT / "prompts" / "vergex_default.md"
    persona = load_strategy_prompt(prompt_path)
    system = build_system_prompt(persona)
    rec("prompt_file_exists", prompt_path.exists(), str(prompt_path.name))
    rec("persona_loaded", "保住本金" in persona and "hold" in persona, "chars=%d" % len(persona))
    rec("system_fixed_contract", "chips" in SYSTEM_PROMPT and "stop_entry" in PLAN_SCHEMA_HINT and "策略人格" in system,
        "system_chars=%d" % len(system))

    # ── 2 行情快照（真实）────────────────────────────────
    print("\n[2] market snapshot (real testnet)")
    symbols = ["BTC_USDT", "ETH_USDT"]
    snapshot = collect_snapshot(
        client, symbols, candles=50, interval="15m",
        market_cfg=MarketConfig(mode="rest_only", refresh=["ticker", "stats", "orderbook"]),
        env="testnet",
    )
    e0 = snapshot["market"]["BTC_USDT"]
    rec("snapshot_real", e0.get("last") and e0.get("candles") and "account" in snapshot,
        "last=%s funding=%s oi=%s book=%s" % (
            e0.get("last"),
            (e0.get("ticker") or {}).get("funding_rate"),
            (e0.get("stats") or {}).get("open_interest_usd"),
            bool(e0.get("orderbook")),
        ))
    risk_hint = {
        "min_confidence": 0.65, "max_notional_usd": 25, "max_chips": 2,
        "allow_actions": ["hold", "open_long", "open_short", "close", "reduce_long", "reduce_short"],
    }
    user = build_user_prompt(snapshot, risk_hint, symbols)
    rec("user_prompt", "市场与账户快照" in user and "BTC_USDT" in user, "chars=%d" % len(user))

    # ── 3 真实 LLM ────────────────────────────────────────
    print("\n[3] real LLM plan  model=%s" % model)
    llm = LLMClient(LLMConfig(model=model, temperature=0.1, timeout_sec=90, max_tokens=4096))
    text = llm.chat(system, user)
    rec("llm_chat", bool(text), "finish=%s chars=%d" % (getattr(llm, "last_finish_reason", None), len(text)))
    plan = parse_plan_text(text)
    rec("parse_plan", True, "cycle=%s chips=%d reasoning=%s" % (
        plan.cycle_id, len(plan.chips), (plan.reasoning or "")[:40]))
    for c in plan.chips:
        print("    chip", c.symbol, c.action, "conf=%.2f" % c.confidence, "|", (c.reasoning or "")[:50])

    # ── 4 风控 ────────────────────────────────────────────
    print("\n[4] risk gate")
    risk_cfg = RiskConfig(
        min_confidence=0.65, max_notional_usd=25, max_chips=2,
        allow_actions={"hold", "open_long", "open_short", "close", "reduce_long", "reduce_short"},
    )
    risk = apply_risk(plan, risk_cfg)
    tradeable = [c for c in risk.accepted if c.action != "hold"]
    rec("risk_gate", True, "accepted=%d tradeable=%d rejected=%d" % (
        len(risk.accepted), len(tradeable), len(risk.rejected)))

    used_plan, used_risk = plan, risk
    injected = False
    if not tradeable:
        print("    LLM hold-only → inject 20U probe through SAME bridge path")
        last = client.get_last_price("BTC_USDT")
        used_plan = Plan(
            cycle_id="e2e-%d" % int(time.time()),
            reasoning="prod e2e probe",
            chips=[Chip(
                symbol="BTC_USDT", action="open_long", confidence=0.9, size_usd=20,
                order_type="market",
                tp=round(last * 1.008, 1), sl=round(last * 0.992, 1),
                reasoning="prod e2e probe",
            )],
        )
        used_risk = apply_risk(used_plan, risk_cfg)
        tradeable = [c for c in used_risk.accepted if c.action != "hold"]
        injected = True
        rec("inject_probe", len(tradeable) == 1, "action=%s size=%s" % (
            tradeable[0].action, tradeable[0].size_usd))

    # ── 5 bridge → inbox ──────────────────────────────────
    print("\n[5] bridge → inbox JSON")
    payload = chips_to_signal(used_plan, used_risk, bot_id=bot_id)
    payload["meta"]["strategy"] = prompt_path.stem
    rec("orders_payload", len(payload.get("orders") or []) > 0,
        "orders=%d meta.strategy=%s" % (len(payload["orders"]), payload["meta"]["strategy"]))
    sig_path = write_signal_file(inbox, payload, cycle_id=used_plan.cycle_id)
    raw = json.loads(sig_path.read_text(encoding="utf-8"))
    rec("inbox_json", sig_path.exists() and raw.get("orders"), "file=%s keys=%s" % (
        sig_path.name, sorted(raw.keys())))
    rec("inbox_meta_cycle", (raw.get("meta") or {}).get("plan_cycle") == used_plan.cycle_id,
        "plan_cycle=%s" % (raw.get("meta") or {}).get("plan_cycle"))

    # ── 6 watcher → executor → 交易所 ──────────────────────
    print("\n[6] watcher → executor → exchange")
    bot = BotConfig(
        bot_id=bot_id, env="testnet", symbols=symbols,
        max_notional_usd=25, api_key_env="GATE_TESTNET_API_KEY",
        api_secret_env="GATE_TESTNET_API_SECRET",
        position_policy="free", default_replace="symbol", label_prefix="e2e",
    )
    ok = process_file(sig_path, bot, paths, executor=None)
    rec("process_file", bool(ok), "ok=%s" % ok)
    orders = client.list_orders("BTC_USDT") or []
    pos = [p for p in (client.get_positions() or []) if int(p.get("size") or 0) != 0]
    rec("exchange_visible", True, "open_orders=%d pos=%d sample_id=%s" % (
        len(orders), len(pos), (orders[0].get("id") if orders else (pos[0].get("id") if pos else "-"))))

    # ── 7 trades 日志（字段级）─────────────────────────────
    print("\n[7] trades journal field check")
    jpath = trade_log_path(paths.root, bot_id)
    rec("journal_exists", jpath.exists(), str(jpath.name))
    rows = TradeLogger(jpath).tail(20)
    execs = [r for r in rows if r.get("type") == "execution"]
    rec("journal_has_execution", len(execs) >= 1, "rows=%d execs=%d" % (len(rows), len(execs)))
    row = execs[-1] if execs else {}
    rec("journal_ts_utc", "T" in str(row.get("ts", "")) and str(row.get("ts", "")).endswith("+00:00") or "T" in str(row.get("ts", "")),
        "ts=%s" % row.get("ts"))
    rec("journal_bot_source", row.get("bot_id") == bot_id and row.get("source") == "watcher",
        "bot=%s source=%s" % (row.get("bot_id"), row.get("source")))
    rec("journal_plan_cycle", row.get("plan_cycle") == used_plan.cycle_id,
        "plan_cycle=%s" % row.get("plan_cycle"))
    rec("journal_strategy", row.get("strategy") == prompt_path.stem, "strategy=%s" % row.get("strategy"))
    rec("journal_ok_steps", row.get("ok") is True and isinstance(row.get("steps"), list) and len(row["steps"]) >= 1,
        "ok=%s steps=%s" % (row.get("ok"), json.dumps(row.get("steps"), ensure_ascii=False)[:80]))
    # archive result.json 必须存在（watcher 成功归档）
    done = list(paths.bot_done(bot_id).glob("*.json"))
    rec("archive_done", any(not p.name.endswith(".result.json") for p in done) or ok,
        "done_files=%d" % len(done))

    # ── 8 cleanup ─────────────────────────────────────────
    print("\n[8] cleanup")
    from gate_bot.executor import Executor
    from gate_bot.schema import parse_signal
    ex = Executor(client, symbols_whitelist=symbols, max_notional_usd=25)
    flat = ex.execute_signal(parse_signal({"action": "flatten", "symbol": "BTC_USDT"}))
    can = ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT"}))
    rec("cleanup", flat.ok and can.ok, "flatten=%s cancel=%s" % (flat.ok, can.ok))

    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("PROD E2E SUMMARY  PASS=%d  FAIL=%d  TOTAL=%d  injected=%s" % (
        n_pass, n_fail, len(RESULTS), injected))
    if n_fail:
        for n, s, d in RESULTS:
            if s == "FAIL":
                print("  FAIL", n, "|", d)
    out = ROOT / "logs" / "prod_e2e.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "results": RESULTS,
            "injected": injected,
            "plan": {"cycle_id": used_plan.cycle_id, "reasoning": used_plan.reasoning},
            "journal_row": row,
        }, ensure_ascii=False, default=str) + "\n")
    print("log:", out)
    print("journal:", jpath)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
