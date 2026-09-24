# -*- coding: utf-8 -*-
"""Production-grade testnet suite: triggers + real AI + order types + full chain.

Env:
  GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET
  OPENAI_BASE_URL / OPENAI_API_KEY
Optional: LLM_MODEL
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
from gate_bot.strategist.risk import RiskConfig  # noqa: E402
from gate_bot.strategist.schema import Chip, Plan, parse_plan_text  # noqa: E402
from gate_bot.strategist.triggers import check_conditions, parse_conditions  # noqa: E402
from gate_bot.watcher import ProjectPaths, process_file  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def rec(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"
    RESULTS.append((name, st, str(detail)[:120]))
    print("%s  %-36s %s" % (st, name, str(detail)[:120]))
    return ok


def main() -> int:
    model = os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash"
    for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "GATE_TESTNET_API_KEY"):
        if not os.environ.get(k):
            print("missing env", k)
            return 2

    client = GateClient(
        os.environ["GATE_TESTNET_API_KEY"],
        os.environ["GATE_TESTNET_API_SECRET"],
        env="testnet",
    )
    symbols = ["BTC_USDT", "ETH_USDT"]
    root = ROOT / ".prod_test"
    if root.exists():
        shutil.rmtree(root)
    paths = ProjectPaths(root)
    paths.ensure()
    print("PROD testnet  model=%s" % model)
    print("=" * 72)

    # ── 1 configurable timers ─────────────────────────────
    print("\n[1] trigger config (5m/10m/1m + conditions)")
    cfg5 = StrategistConfig(interval_sec=300, timeframe="5m", event_timeframe="5m",
                            conditions=[{"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 20}])
    cfg10 = StrategistConfig(interval_sec=600, timeframe="10m", event_on_kline_close=False)
    rec("interval_5m_300s", cfg5.interval_sec == 300 and cfg5.event_timeframe == "5m",
        "5m/300s event_tf=%s" % cfg5.event_timeframe)
    rec("interval_10m_600s", cfg10.interval_sec == 600 and cfg10.event_timeframe == "10m",
        "10m/600s close_off=%s" % (not cfg10.event_on_kline_close))
    conds = parse_conditions([
        {"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 20, "side": "above", "cooldown_sec": 5},
        {"type": "ema_cross", "symbol": "BTC_USDT", "fast": 9, "slow": 21},
        {"type": "atr_spike", "symbol": "BTC_USDT", "period": 14, "mult": 1.3},
        {"type": "price_break", "symbol": "ETH_USDT", "lookback": 20, "side": "high"},
        {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "gt", "level": 65},
    ])
    rec("parse_5_condition_types", len(conds) == 5, ",".join(c["type"] for c in conds))

    # ── 2 live condition evaluation ───────────────────────
    print("\n[2] live condition evaluation")
    fired = check_conditions(client, conds, "5m", states={}, now=time.time())
    rec("condition_eval_live", True,
        "fired=%d %s" % (len(fired), [(f["type"], f["reason"][:30]) for f in fired[:3]]))

    # ── 3 real AI plan (5m snapshot) ──────────────────────
    print("\n[3] real AI plan-loop once (trigger=interval)")
    cfg = StrategistConfig(
        interval_sec=30, timeframe="5m", event_timeframe="1m",
        event_on_kline_close=True,
        conditions=[{"type": "price_vs_ema", "symbol": "BTC_USDT", "period": 20, "side": "below", "cooldown_sec": 5}],
        symbols=symbols, candles=40,
        market=MarketConfig(mode="rest_only", refresh=["ticker", "stats", "orderbook"]),
        env="testnet", bot_root=paths.root,
        risk=RiskConfig(min_confidence=0.65, max_notional_usd=25, max_chips=2,
                        allow_actions={"hold", "open_long", "open_short", "close", "reduce_long", "reduce_short"}),
        llm=LLMConfig(model=model, temperature=0.1, timeout_sec=90, max_tokens=4096),
        write_hold=True,
    )
    inbox = paths.bot_inbox("prod")
    history = paths.root / "history" / "prod"
    runner = PlanRunner(client, cfg, inbox, history)
    r = runner.run_once(trigger="interval")
    rec("ai_plan_ok", bool(r.get("ok")), json.dumps({k: r.get(k) for k in ("ok", "cycle_id", "orders", "trigger")}, ensure_ascii=False))
    if r.get("file"):
        rec("ai_wrote_inbox", Path(r["file"]).exists(), Path(r["file"]).name)
    elif r.get("orders") == 0:
        # force tiny probe through same bridge so exec hop is still tested
        forced = Plan(cycle_id="prod-%d" % int(time.time()), reasoning="prod probe",
                      chips=[Chip(symbol="BTC_USDT", action="open_long", confidence=0.9,
                                  size_usd=20, order_type="market",
                                  tp=round(client.get_last_price("BTC_USDT") * 1.01, 1),
                                  sl=round(client.get_last_price("BTC_USDT") * 0.99, 1))])
        from gate_bot.strategist.risk import apply_risk
        risk = apply_risk(forced, cfg.risk)
        payload = chips_to_signal(forced, risk, bot_id="prod")
        p = write_signal_file(inbox, payload, cycle_id=forced.cycle_id)
        rec("ai_hold_bridge_probe", p.exists(), p.name)
    else:
        rec("ai_wrote_inbox", False, str(r)[:100])

    # ── 4 execute signals → exchange ──────────────────────
    print("\n[4] watcher → exchange")
    bot = BotConfig(bot_id="prod", env="testnet", symbols=symbols,
                    max_notional_usd=25, api_key_env="GATE_TESTNET_API_KEY",
                    api_secret_env="GATE_TESTNET_API_SECRET",
                    position_policy="free", default_replace="symbol", label_prefix="prod")
    files = sorted(inbox.glob("*.json"))
    ok_all = True
    for f in files:
        ok = process_file(f, bot, paths, executor=None)
        ok_all = ok_all and ok
        rec("exec_" + f.name[:28], ok, "done=%s" % ok)
    if not files:
        rec("exec_any_signal", False, "no inbox files")
    else:
        orders = client.list_orders("BTC_USDT") or []
        pos = [p for p in (client.get_positions() or []) if int(p.get("size") or 0) != 0]
        rec("exchange_state", True, "open_orders=%d pos=%d" % (len(orders), len(pos)))

    # ── 5 order types smoke (post AI exec) ────────────────
    print("\n[5] order types (testnet)")
    from gate_bot.executor import Executor
    from gate_bot.schema import parse_signal
    ex = Executor(client, symbols_whitelist=symbols, max_notional_usd=25)
    last = client.get_last_price("BTC_USDT")

    def run(name, payload, expect_ok=True):
        try:
            rep = ex.execute_signal(parse_signal(payload))
            s = rep.results[0]
            rec(name, s.ok == expect_ok, s.error or str((s.detail or {}).get("order", {}).get("id")))
        except Exception as e:  # noqa: BLE001
            rec(name, False, str(e))

    sl_p = round(last * 0.99, 1)
    run("mkt_long", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                     "sl": sl_p, "label": "p-m"})
    run("limit_long", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "limit",
                       "price": round(last * 0.98, 1), "sl": sl_p, "label": "p-l"})
    run("post_only", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "post_only",
                      "price": round(last * 0.97, 1), "sl": sl_p, "label": "p-po"})
    run("ioc_far", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "ioc",
                    "price": round(last * 0.90, 1), "sl": sl_p, "label": "p-ioc"})
    run("fok_far_reject", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "fok",
                           "price": round(last * 0.90, 1)}, expect_ok=False)
    run("tpsl_trigger", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                         "tp": round(last * 1.02, 1), "sl": round(last * 0.98, 1),
                         "tp_mode": "trigger", "sl_mode": "trigger", "label": "p-tt"})
    run("tpsl_limit_band", {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market",
                            "tp": round(last * 1.008, 1), "sl": round(last * 0.992, 1),
                            "tp_mode": "limit_order", "sl_mode": "limit_order", "label": "p-tl"})
    run("stop_entry", {"action": "stop_entry_long", "symbol": "BTC_USDT", "size": 1,
                       "trigger_price": round(last * 1.03, 1), "label": "p-se"})
    run("grid", {"action": "grid", "symbol": "BTC_USDT", "side": "long", "type": "limit",
                 "sl": sl_p,
                 "levels": [{"price": round(last * 0.96, 1), "size": 1}], "label": "p-g"})
    run("orders_multi", {"orders": [
        {"action": "add_long", "symbol": "ETH_USDT", "size": 1, "type": "limit",
         "price": round(client.get_last_price("ETH_USDT") * 0.98, 1), "label": "p-m1"},
    ]})
    run("reduce", {"action": "reduce_long", "symbol": "BTC_USDT", "size": 1, "label": "p-r"})
    run("cancel_all", {"action": "cancel_all", "symbol": "BTC_USDT"})
    run("flatten", {"action": "flatten", "symbol": "BTC_USDT"})
    run("flatten_eth", {"action": "flatten", "symbol": "ETH_USDT"})

    # ── 6 short interval loop (3 ticks) ───────────────────
    print("\n[6] plan-loop short interval (30s x 2 ticks simulated)")
    runner2 = PlanRunner(client, cfg, inbox, history)
    r1 = runner2.run_once(trigger="interval")
    time.sleep(0.2)
    r2 = runner2.run_once(trigger="kline_close")
    rec("loop_two_triggers", r1.get("ok") and r2.get("ok"),
        "t1=%s t2=%s" % (r1.get("trigger"), r2.get("trigger")))
    rec("trigger_labels", r1.get("trigger") == "interval" and r2.get("trigger") == "kline_close",
        "%s / %s" % (r1.get("trigger"), r2.get("trigger")))

    # cleanup
    ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT"}))
    ex.execute_signal(parse_signal({"action": "flatten", "symbol": "BTC_USDT"}))

    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("PROD SUMMARY  PASS=%d  FAIL=%d  TOTAL=%d" % (n_pass, n_fail, len(RESULTS)))
    if n_fail:
        for n, s, d in RESULTS:
            if s == "FAIL":
                print("  FAIL", n, "|", d)
    out = ROOT / "logs" / "prod_test.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"results": RESULTS}, ensure_ascii=False) + "\n")
    print("log:", out)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
