# -*- coding: utf-8 -*-
"""Real LLM sizing-mode probe: size_usd / size / size_pct / margin_pct confusion.

Env (never hardcode keys):
  OPENAI_BASE_URL, OPENAI_API_KEY
Optional:
  LLM_MODEL (default global:deepseek-v4.1-flash)
  GATE_API_KEY / GATE_API_SECRET  (live snapshot; else stub candles + fake contract)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.sizing import usd_to_contracts  # noqa: E402
from omnialpha.strategist.llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402
from omnialpha.strategist.market import MarketConfig  # noqa: E402
from omnialpha.strategist.prompt import build_system_prompt, load_strategy_prompt  # noqa: E402
from omnialpha.strategist.schema import PlanError, parse_plan_text  # noqa: E402
from omnialpha.strategist.snapshot import collect_snapshot  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    RESULTS.append((name, status, str(detail)[:160]))
    print("%s  %-38s %s" % (status, name, str(detail)[:160]))
    return ok


def _chip_fields(raw: dict) -> set[str]:
    return {k for k in raw if k not in ("symbol", "action", "confidence", "tp", "sl",
                                        "type", "price", "trigger_price", "reasoning")}


def extract_raw_chips(text: str) -> list[dict]:
    """Best-effort raw chips list from model text (before schema filter)."""
    try:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.M)
        data = json.loads(cleaned)
        chips = data.get("chips")
        return chips if isinstance(chips, list) else []
    except Exception:  # noqa: BLE001
        m = re.search(r"\{[\s\S]*\}", text or "")
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
            chips = data.get("chips")
            return chips if isinstance(chips, list) else []
        except Exception:  # noqa: BLE001
            return []


def build_snapshot(symbols: list[str]) -> dict:
    key = os.environ.get("GATE_API_KEY") or ""
    sec = os.environ.get("GATE_API_SECRET") or ""
    if key and sec:
        client = GateClient(key, sec, env="live")
        env = "live"
        mcfg = MarketConfig(mode="rest_only", refresh=["ticker"])
    else:
        class _Stub:
            def get_ticker(self, sym):
                return {"last": "100", "mark_price": "100", "index_price": "100",
                        "funding_rate": "0.0001", "high_24h": "101", "low_24h": "99",
                        "change_percentage": "1", "change_price": "1",
                        "volume_24h_quote": "1", "highest_bid": "99.9",
                        "lowest_ask": "100.1", "total_size": "1"}

            def get_contract(self, sym):
                from omnialpha.gate_client import ContractMeta
                return ContractMeta(sym, 0.0001, 1.0, 0.1, 100)

            def get_last_price(self, sym):
                return 100.0

            def get_account(self):
                return {"position_mode": "single", "available": "10000", "total": "10000"}

            def get_positions(self):
                return []

        client = _Stub()
        env = "testnet"
        mcfg = MarketConfig(mode="rest_only", refresh=["ticker"])
    return collect_snapshot(
        client, symbols, candles=30, interval="15m",
        market_cfg=mcfg, env=env,
    )


CASES = [
    {
        "name": "A_size_usd_default",
        "instruction": "对 BTC_USDT 开多，名义仓位 30 USDT（size_usd=30），必须带 sl。",
        "expect_fields": {"size_usd"},
        "forbid_fields": {"size", "size_pct", "margin_pct"},
        "check": "prefer_size_usd",
    },
    {
        "name": "B_size_contracts",
        "instruction": "对 BTC_USDT 开多，只用合约张数 size=2（不要写 size_usd），必须带 sl。"
                      "先查快照 contract 确认 1 张名义后写 size。",
        "expect_fields": {"size"},
        "forbid_fields": {"size_usd", "size_pct", "margin_pct"},
        "check": "size_is_contracts",
    },
    {
        "name": "C_size_pct_maybe",
        "instruction": "对 ETH_USDT 开多，用可用余额的 5% 仓位（若支持请写 size_pct=0.05），必须带 sl。"
                      "若快照 contract 显示该仓位不足最小名义，请直接 hold 并说明。",
        "expect_fields": set(),  # flexible
        "forbid_fields": set(),
        "check": "pct_handling",
    },
    {
        "name": "D_margin_pct_maybe",
        "instruction": "对 SOL_USDT 开多，保证金占可用 2% 并 5 倍杠杆（若支持请写 margin_pct=0.02 与 leverage=5），必须带 sl。"
                      "若 contract.min_notional_usd 超过 risk.max_notional_usd，请 hold 并说明。",
        "expect_fields": set(),
        "forbid_fields": set(),
        "check": "margin_handling",
    },
    {
        "name": "F_pct_to_usd_forced",
        "instruction": "对 BTC_USDT 开多。不要输出 size_pct/margin_pct/size，只输出 size_usd=40（名义 40U），必须带 sl。",
        "expect_fields": {"size_usd"},
        "forbid_fields": {"size", "size_pct", "margin_pct"},
        "check": "prefer_size_usd",
    },
    {
        "name": "G_below_min_notional",
        "instruction": "对 SOL_USDT 开多 size_usd=20（故意低于 1 张最小名义），必须带 sl。"
                      "若 contract.min_notional_usd > 20，请 hold 并说明，不要硬开。",
        "expect_fields": set(),
        "forbid_fields": set(),
        "check": "smart_hold_or_size",
    },
    {
        "name": "E_mixed_icons",
        "instruction": "同时：BTC_USDT 开多 size_usd=25；ETH_USDT 开空 size=1；SOL_USDT 开多 size_usd=15。每笔必须带 sl。",
        "expect_fields": {"size_usd"},
        "forbid_fields": {"size_pct", "margin_pct"},
        "check": "mixed",
    },
]


def main() -> int:
    base = os.environ.get("OPENAI_BASE_URL") or ""
    key = os.environ.get("OPENAI_API_KEY") or ""
    model = os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash"
    if not base or not key:
        print("missing OPENAI_BASE_URL / OPENAI_API_KEY")
        return 2

    strategy = load_strategy_prompt(ROOT / "prompts" / "vergex_default.md")
    system = build_system_prompt(strategy)
    rec("system_has_size_rule", "size_usd" in system and "min_notional_usd" in system,
        "rule3 + contract meta guidance present")

    symbols = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]
    print("\n[1] snapshot")
    snap = build_snapshot(symbols)
    for s in symbols:
        c = (snap["market"].get(s) or {}).get("contract") or {}
        print("   ", s, "quanto=", c.get("quanto_multiplier"),
              "min_notional=", c.get("min_notional_usd"))
    user_base = (
        "【品种宇宙】\n" + json.dumps(symbols, ensure_ascii=False)
        + "\n\n【策略风控】\n"
        + json.dumps({"min_confidence": 0.3, "max_notional_usd": 50, "max_chips": 3,
                      "allow_actions": ["open_long", "open_short", "hold"]},
                     ensure_ascii=False)
        + "\n\n【市场与账户快照】\n"
        + json.dumps(snap, ensure_ascii=False)
        + "\n\n"
    )

    llm = LLMClient(LLMConfig(model=model, temperature=0.1, timeout_sec=90, max_tokens=4096))
    overall = True

    print("\n[2] LLM sizing cases model=%s" % model)
    for case in CASES:
        user = user_base + "【本轮任务】" + case["instruction"] + "\n请输出本轮 Plan JSON。"
        t0 = time.time()
        try:
            text = llm.chat(system, user)
        except LLMError as e:
            rec(case["name"], False, "llm_error %s" % e)
            overall = False
            continue
        latency = time.time() - t0
        raw_chips = extract_raw_chips(text)
        field_sets = [_chip_fields(c) for c in raw_chips]
        used = set().union(*field_sets) if field_sets else set()
        print("    raw_head:", (text or "")[:180].replace("\n", " "))
        print("    chips:", len(raw_chips), "size_fields=", sorted(used),
              "latency=%.1fs" % latency)

        try:
            plan = parse_plan_text(text)
            parse_ok = True
            parse_detail = "chips=%d" % len(plan.chips)
        except PlanError as e:
            parse_ok = False
            plan = None
            parse_detail = str(e)
            overall = False

        rec(case["name"] + ":parse", parse_ok, parse_detail)

        if not parse_ok:
            continue

        size_chips = [c for c in plan.chips if c.action.startswith(("open", "add", "stop_entry"))]
        has_size = [c for c in size_chips if c.size_usd is not None or c.size is not None]
        hold_ok_checks = {"pct_handling", "margin_handling", "smart_hold_or_size"}
        if case["check"] in hold_ok_checks and not size_chips:
            rec(case["name"] + ":has_sizing", True, "no entry chips (smart hold allowed)")
        else:
            rec(case["name"] + ":has_sizing", bool(size_chips) and len(has_size) == len(size_chips),
                "entry_chips=%d with_size=%d fields=%s" % (len(size_chips), len(has_size), sorted(used)))

        # forbid unexpected field types that schema silently drops
        if case["forbid_fields"] & used:
            # schema drops them — if also no size_usd/size, that's a real bug
            only_dropped = (used <= case["forbid_fields"]) and not any(
                c.size_usd is not None or c.size is not None for c in size_chips
            )
            rec(case["name"] + ":no_dropped_only", not only_dropped,
                "used=%s (schema may drop)" % sorted(used))
            if only_dropped:
                overall = False
        else:
            rec(case["name"] + ":no_forbidden", True, "used=%s" % sorted(used))

        if case["check"] == "prefer_size_usd":
            ok = all(c.size_usd is not None for c in size_chips) and all(c.size is None for c in size_chips)
            rec(case["name"] + ":size_usd_only", ok and bool(size_chips),
                "chips=" + json.dumps([(c.symbol, c.size_usd, c.size) for c in size_chips]))
            if not ok:
                overall = False
        elif case["check"] == "size_is_contracts":
            ok = bool(size_chips) and all(c.size is not None and c.size_usd is None for c in size_chips)
            # verify size against contract min unit if possible
            detail_parts = []
            for c in size_chips:
                cm = (snap["market"].get(c.symbol) or {}).get("contract") or {}
                last = (snap["market"].get(c.symbol) or {}).get("last") or 0
                q = cm.get("quanto_multiplier") or 1
                notional = (c.size or 0) * float(last) * float(q)
                detail_parts.append("%s size=%s ≈%.2fU" % (c.symbol, c.size, notional))
            rec(case["name"] + ":size_contracts", ok,
                "; ".join(detail_parts) or "no chips")
            if not ok:
                overall = False
        elif case["check"] == "pct_handling":
            # size_pct is NOT a Plan field. Success = either resolved to size_usd/size,
            # or smart hold when min_notional makes the request untradeable.
            holds = [c for c in plan.chips if c.action == "hold"]
            sized = [c for c in size_chips if c.size_usd is not None or c.size is not None]
            invented_pct = any("size_pct" in fs or "margin_pct" in fs for fs in field_sets)
            ok = (bool(sized) or (holds and not size_chips)) and not invented_pct
            rec(case["name"] + ":pct_resolved_or_hold", ok,
                "used=%s sized=%s holds=%d invented_pct=%s" % (
                    sorted(used),
                    [(c.symbol, c.size_usd, c.size) for c in sized],
                    len(holds), invented_pct))
            if not ok:
                overall = False
        elif case["check"] == "margin_handling":
            holds = [c for c in plan.chips if c.action == "hold"]
            sized = [c for c in size_chips if c.size_usd is not None or c.size is not None]
            invented_pct = any("size_pct" in fs or "margin_pct" in fs for fs in field_sets)
            ok = (bool(sized) or (holds and not size_chips)) and not invented_pct
            rec(case["name"] + ":margin_resolved_or_hold", ok,
                "used=%s sized=%s holds=%d invented_pct=%s" % (
                    sorted(used),
                    [(c.symbol, c.size_usd, c.size) for c in sized],
                    len(holds), invented_pct))
            if not ok:
                overall = False
        elif case["check"] == "smart_hold_or_size":
            holds = [c for c in plan.chips if c.action == "hold"]
            sized = [c for c in size_chips if c.size_usd is not None or c.size is not None]
            # SOL min_notional ~117U; size_usd=20 cannot fill 1 contract → hold is correct
            ok = bool(holds) or bool(sized)
            rec(case["name"] + ":hold_or_size", ok,
                "holds=%d sized=%s" % (len(holds),
                    [(c.symbol, c.size_usd, c.size) for c in sized]))
            if not ok:
                overall = False
        elif case["check"] == "mixed":
            usd_n = sum(1 for c in size_chips if c.size_usd is not None)
            sz_n = sum(1 for c in size_chips if c.size is not None)
            ok = usd_n >= 1 and sz_n >= 1 and len(size_chips) >= 2
            rec(case["name"] + ":mixed_modes", ok,
                "size_usd_chips=%d size_chips=%d %s" % (
                    usd_n, sz_n,
                    [(c.symbol, c.size_usd, c.size) for c in size_chips]))
            if not ok:
                overall = False

        # convert size_usd → contracts against live meta when available
        for c in size_chips:
            if c.size_usd is None:
                continue
            cm = (snap["market"].get(c.symbol) or {}).get("contract") or {}
            last = (snap["market"].get(c.symbol) or {}).get("last")
            if not cm or not last:
                continue
            try:
                from omnialpha.gate_client import ContractMeta
                meta = ContractMeta(c.symbol, cm["quanto_multiplier"], cm["order_size_round"],
                                    cm["order_price_round"], cm["leverage_max"])
                n = usd_to_contracts(float(c.size_usd), float(last), meta)
                print("    convert", c.symbol, "size_usd=", c.size_usd, "->", n, "contracts")
            except Exception as e:  # noqa: BLE001
                rec(case["name"] + ":convert_" + c.symbol, False, str(e))
                overall = False

    print("\n[3] summary")
    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("PASS=%d FAIL=%d" % (n_pass, n_fail))
    for name, st, detail in RESULTS:
        if st == "FAIL":
            print("  FAIL", name, detail)
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
