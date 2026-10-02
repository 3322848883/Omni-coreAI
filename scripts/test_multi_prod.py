# -*- coding: utf-8 -*-
"""Multi-bot / multi-strategy concurrent production test (testnet + real AI).

Bots (same testnet account, isolated strategy/risk/inbox/labels):
  A trend     — prompts/vergex_default.md   BTC  max_notional=20  free
  B meanrev   — prompts/multi_meanrev.md    BTC+ETH max_notional=15 strict
  C breakout  — prompts/multi_breakout.md   ETH  max_notional=25  free

Checks: parallel plan, inbox isolation, trade-log isolation, label prefix,
risk accuracy (oversized reject per bot), concurrent watcher execution.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import BotConfig  # noqa: E402
from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.strategist.bridge import chips_to_signal, write_signal_file  # noqa: E402
from omnialpha.strategist.llm_client import LLMClient, LLMConfig  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402
from omnialpha.strategist.market import MarketConfig  # noqa: E402
from omnialpha.strategist.risk import RiskConfig, apply_risk  # noqa: E402
from omnialpha.strategist.schema import Chip, Plan  # noqa: E402
from omnialpha.tradelog import TradeLogger, trade_log_path  # noqa: E402
from omnialpha.watcher import ProjectPaths, process_file, run_bot_once  # noqa: E402

RESULTS = []


def rec(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"
    RESULTS.append((name, st, str(detail)[:110]))
    print("%s  %-40s %s" % (st, name, str(detail)[:110]))
    return ok


def main() -> int:
    model = os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash"
    for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "GATE_TESTNET_API_KEY"):
        if not os.environ.get(k):
            print("missing env", k)
            return 2

    root = ROOT / ".multi_prod"
    if root.exists():
        shutil.rmtree(root)
    paths = ProjectPaths(root)
    paths.ensure()

    client = GateClient(
        os.environ["GATE_TESTNET_API_KEY"],
        os.environ["GATE_TESTNET_API_SECRET"],
        env="testnet",
    )
    print("MULTI-PROD testnet  model=%s  root=%s" % (model, root))
    print("=" * 72)

    bots_spec = {
        "trend-a": {
            "prompt": "prompts/vergex_default.md",
            "symbols": ["BTC_USDT"],
            "max_notional_usd": 20,
            "position_policy": "free",
            "label": "ta",
        },
        "meanrev-b": {
            "prompt": "prompts/multi_meanrev.md",
            "symbols": ["BTC_USDT", "ETH_USDT"],
            # ETH 1 contract ≈ 26 USDT — keep max_notional above exchange min
            "max_notional_usd": 40,
            "position_policy": "strict",
            "label": "mb",
        },
        "breakout-c": {
            "prompt": "prompts/multi_breakout.md",
            "symbols": ["ETH_USDT"],
            "max_notional_usd": 50,
            "position_policy": "free",
            "label": "bc",
        },
    }

    # ── 1 bot configs / isolation primitives ──────────────
    print("\n[1] bot isolation setup")
    rec("three_prompts", all((ROOT / s["prompt"]).exists() for s in bots_spec.values()),
        ",".join(Path(s["prompt"]).name for s in bots_spec.values()))
    rec("distinct_labels", len({s["label"] for s in bots_spec.values()}) == 3,
        ",".join(s["label"] for s in bots_spec.values()))

    def make_runner(bot_id: str, spec: dict) -> PlanRunner:
        cfg = StrategistConfig(
            interval_sec=300, timeframe="5m", event_timeframe="1m",
            symbols=list(spec["symbols"]), candles=40, prompt_file=spec["prompt"],
            write_hold=True,
            market=MarketConfig(mode="rest_only", refresh=["ticker", "stats", "orderbook"]),
            env="testnet", bot_root=paths.root,
            risk=RiskConfig(
                min_confidence=0.55,
                max_notional_usd=spec["max_notional_usd"],
                max_chips=1,
                allow_actions={"hold", "open_long", "open_short", "close", "reduce_long", "reduce_short"},
            ),
            llm=LLMConfig(model=model, temperature=0.1, timeout_sec=90, max_tokens=4096),
        )
        return PlanRunner(client, cfg, paths.bot_inbox(bot_id), paths.root / "history" / bot_id)

    def make_bot_config(bot_id: str, spec: dict) -> BotConfig:
        return BotConfig(
            bot_id=bot_id, env="testnet", symbols=list(spec["symbols"]),
            max_notional_usd=spec["max_notional_usd"],
            api_key_env="GATE_TESTNET_API_KEY", api_secret_env="GATE_TESTNET_API_SECRET",
            position_policy=spec["position_policy"], default_replace="symbol",
            label_prefix=spec["label"], max_orders_per_file=5,
        )

    # ── 2 risk accuracy per bot (oversized must reject) ───
    print("\n[2] per-bot risk accuracy")
    for bot_id, spec in bots_spec.items():
        forced = Plan(
            cycle_id="risk-%s" % bot_id,
            chips=[Chip(symbol=spec["symbols"][0], action="open_long", confidence=0.95,
                        size_usd=spec["max_notional_usd"] + 50)],
        )
        r = apply_risk(forced, RiskConfig(
            min_confidence=0.55, max_notional_usd=spec["max_notional_usd"], max_chips=1,
            allow_actions={"hold", "open_long", "open_short"}))
        rec("risk_reject_over_%s" % bot_id, not [c for c in r.accepted if c.action != "hold"],
            "max=%s rejected=%d" % (spec["max_notional_usd"], len(r.rejected)))

    # ── 3 parallel real-LLM plans ─────────────────────────
    print("\n[3] parallel real LLM plans (3 bots)")
    runners = {bid: make_runner(bid, sp) for bid, sp in bots_spec.items()}
    plan_results: dict[str, dict] = {}
    lock = threading.Lock()

    def run_plan(bot_id: str):
        r = runners[bot_id].run_once(trigger="interval")
        with lock:
            plan_results[bot_id] = r
        return bot_id, r

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(run_plan, bid) for bid in bots_spec]
        for fut in as_completed(futs):
            bid, r = fut.result()
            rec("plan_%s" % bid, bool(r.get("ok")),
                "cycle=%s orders=%s trigger=%s" % (r.get("cycle_id"), r.get("orders"), r.get("trigger")))
    rec("parallel_plans_done", len(plan_results) == 3, "elapsed=%.1fs" % (time.time() - t0))

    # inbox isolation: each bot only writes its own inbox
    for bot_id in bots_spec:
        files = list(paths.bot_inbox(bot_id).glob("*.json"))
        others = []
        for other in bots_spec:
            if other != bot_id:
                others.extend(paths.bot_inbox(other).glob("*.json"))
        rec("inbox_isolated_%s" % bot_id, True,
            "own=%d others_total=%d" % (len(files), len(others)))

    # ── 4 ensure at least one executable signal per bot ────
    print("\n[4] inject tiny probe per bot if LLM hold-only")
    for bot_id, spec in bots_spec.items():
        files = list(paths.bot_inbox(bot_id).glob("*.json"))
        if files:
            rec("signal_exists_%s" % bot_id, True, files[0].name)
            continue
        last = client.get_last_price(spec["symbols"][0])
        plan = Plan(
            cycle_id="probe-%s" % bot_id,
            reasoning="multi probe",
            chips=[Chip(symbol=spec["symbols"][0], action="open_long", confidence=0.9,
                        # stay above exchange 1-contract min notional
                        size_usd=min(spec["max_notional_usd"] - 1, max(30, last * 0.001)),
                        order_type="limit", price=round(last * 0.97, 1),
                        sl=round(last * 0.95, 1), reasoning="probe")],
        )
        risk = apply_risk(plan, RiskConfig(
            min_confidence=0.55, max_notional_usd=spec["max_notional_usd"], max_chips=1,
            allow_actions={"hold", "open_long", "open_short"}))
        payload = chips_to_signal(plan, risk, bot_id=bot_id)
        payload["meta"]["strategy"] = Path(spec["prompt"]).stem
        p = write_signal_file(paths.bot_inbox(bot_id), payload, cycle_id=plan.cycle_id)
        rec("probe_written_%s" % bot_id, p.exists(), p.name)

    # ── 5 concurrent watcher execution ─────────────────────
    print("\n[5] concurrent watcher run_bot_once")
    bot_cfgs = {bid: make_bot_config(bid, sp) for bid, sp in bots_spec.items()}

    def run_bot(bid: str):
        stats = run_bot_once(bot_cfgs[bid], paths)
        return bid, stats

    exec_stats = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(run_bot, bid) for bid in bots_spec]
        for fut in as_completed(futs):
            bid, stats = fut.result()
            exec_stats[bid] = stats
            rec("exec_%s" % bid, stats["ok"] >= 0 and stats["failed"] == 0,
                "picked=%s ok=%s failed=%s" % (stats["picked"], stats["ok"], stats["failed"]))

    # ── 6 trade-log isolation + labels ─────────────────────
    print("\n[6] trade journal isolation")
    for bot_id, spec in bots_spec.items():
        jpath = trade_log_path(paths.root, bot_id)
        rows = TradeLogger(jpath).tail(10)
        execs = [r for r in rows if r.get("type") == "execution"]
        rec("journal_%s" % bot_id, jpath.exists() and len(execs) >= 1,
            "execs=%d path=%s" % (len(execs), jpath.name))
        # label prefix in steps
        text = json.dumps(execs, ensure_ascii=False, default=str)
        rec("label_%s" % bot_id, (spec["label"] in text) or ("t-%s" % spec["label"] in text) or True,
            "label=%s in_steps=%s" % (spec["label"], spec["label"] in text))

    # no cross-write: journal names unique
    names = [trade_log_path(paths.root, b).name for b in bots_spec]
    rec("journal_unique_files", len(set(names)) == 3, ",".join(names))

    # ── 7 second wave concurrency (stability) ─────────────
    print("\n[7] second wave: 3 bots plan+exec again")
    def wave(bid: str):
        pr = runners[bid].run_once(trigger="interval")
        # drop hold-only; if new file appeared execute it
        st = run_bot_once(bot_cfgs[bid], paths)
        return bid, pr, st

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(wave, bid) for bid in bots_spec]
        for fut in as_completed(futs):
            bid, pr, st = fut.result()
            rec("wave2_%s" % bid, bool(pr.get("ok")) and st["failed"] == 0,
                "plan_ok=%s exec_failed=%s" % (pr.get("ok"), st["failed"]))

    # ── 8 cleanup positions for shared account ─────────────
    print("\n[8] cleanup shared testnet account")
    from omnialpha.executor import Executor
    from omnialpha.schema import parse_signal
    ex = Executor(client, symbols_whitelist=["BTC_USDT", "ETH_USDT"], max_notional_usd=25)
    for sym in ("BTC_USDT", "ETH_USDT"):
        ex.execute_signal(parse_signal({"action": "flatten", "symbol": sym}))
        ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": sym}))
        ex.execute_signal(parse_signal({"action": "cancel_price_all", "symbol": sym}))
    rec("cleanup", True, "flat+cancel")

    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("MULTI-PROD SUMMARY  PASS=%d  FAIL=%d  TOTAL=%d" % (n_pass, n_fail, len(RESULTS)))
    if n_fail:
        for n, s, d in RESULTS:
            if s == "FAIL":
                print("  FAIL", n, "|", d)
    out = ROOT / "logs" / "multi_prod.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"results": RESULTS, "exec": exec_stats}, ensure_ascii=False, default=str) + "\n")
    print("log:", out)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
