# -*- coding: utf-8 -*-
"""A/B: snapshot dump vs on-demand tools — quality + tokens + source (db/rest).

Env: OPENAI_BASE_URL, OPENAI_API_KEY; GATE_* or GATE_TESTNET_* for market.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.gate_client import GateClient
from gate_bot.strategist.llm_client import LLMClient, LLMConfig
from gate_bot.strategist.market import MarketConfig
from gate_bot.strategist.prompt import build_system_prompt, load_strategy_prompt
from gate_bot.strategist.risk import RiskConfig, apply_risk
from gate_bot.strategist.schema import PlanError, parse_plan_text
from gate_bot.strategist.snapshot import collect_snapshot
from gate_bot.strategist.tools import TOOL_GUIDE, extract_tool_calls, run_tool
from gate_bot.strategist.prompt import build_user_prompt


def make_client():
    if os.environ.get("GATE_API_KEY"):
        return GateClient(os.environ["GATE_API_KEY"], os.environ.get("GATE_API_SECRET", ""), env="live"), "live"
    return GateClient(
        os.environ.get("GATE_TESTNET_API_KEY", ""),
        os.environ.get("GATE_TESTNET_API_SECRET", ""),
        env="testnet",
    ), "testnet"


def usage_tokens(llm):
    u = getattr(llm, "last_usage", {}) or {}
    return {
        "prompt": u.get("prompt_tokens"),
        "completion": u.get("completion_tokens"),
        "total": u.get("total_tokens"),
    }


def score_plan(plan, text: str) -> dict:
    """Heuristic quality score (0-5)."""
    if plan is None:
        return {"score": 0, "notes": ["parse fail"]}
    notes = []
    score = 0
    chips = plan.chips
    if chips:
        score += 1
        notes.append(f"chips={len(chips)}")
    sized = [c for c in chips if c.size_usd or c.size]
    if chips and all((c.size_usd or c.size) for c in chips if c.action.startswith(("open", "add"))):
        score += 1
        notes.append("has_size")
    sl_ok = any(c.sl for c in chips)
    if sl_ok or any(c.action == "hold" for c in chips):
        score += 1
        notes.append("sl_or_hold")
    # multi-tf / tool awareness in reasoning
    blob = (text or "") + " " + " ".join(c.reasoning or "" for c in chips)
    if any(x in blob for x in ("4h", "4H", "1h", "日线", "多周期", "tf", "工具")):
        score += 1
        notes.append("tf_aware")
    if "区间" in blob or "range" in blob.lower():
        score += 1
        notes.append("range")
    return {"score": score, "notes": notes}


def run_mode_a_snapshot(llm, client, env, prompt_text, symbols):
    """Dump richer snapshot (all TFs) into one prompt."""
    t0 = time.time()
    cfg = MarketConfig(mode="rest_only", extra_timeframes=["all"], extra_candles=12, indicators=["all"])
    snap = collect_snapshot(client, symbols, candles=40, interval="15m", market_cfg=cfg, env=env)
    user = build_user_prompt(
        snap,
        {"min_confidence": 0.5, "max_notional_usd": 50, "max_chips": 1},
        symbols,
    )
    system = build_system_prompt(prompt_text)
    n_chars = len(system) + len(user)
    text = llm.chat(system, user)
    return text, {
        "mode": "A_snapshot_dump",
        "latency_s": round(time.time() - t0, 2),
        "prompt_chars": n_chars,
        "snap_bytes": len(json.dumps(snap, ensure_ascii=False)),
        "llm_rounds": 1,
        "tool_calls": 0,
        "usage": usage_tokens(llm),
    }


def run_mode_b_tools(llm, client, env, prompt_text, symbols, bot_root=None):
    """Compact snapshot + on-demand tools."""
    t0 = time.time()
    cfg = MarketConfig(mode="rest_only", extra_timeframes=[], extra_candles=12,
                       indicators=["ema20", "ema50", "atr14", "rsi14"])
    snap = collect_snapshot(client, symbols, candles=30, interval="15m", market_cfg=cfg, env=env)
    user = build_user_prompt(
        snap,
        {"min_confidence": 0.5, "max_notional_usd": 50, "max_chips": 1},
        symbols,
    )
    system = build_system_prompt(prompt_text) + "\n\n" + TOOL_GUIDE
    n_chars = len(system) + len(user)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    text = llm.chat_messages(messages)
    tool_calls = 0
    rounds = 1
    sources = []
    while True:
        calls = extract_tool_calls(text)
        if not calls or rounds >= 4:
            break
        results = []
        for c in calls[:6]:
            name = c.get("tool") or ""
            args = c.get("args") or {}
            tool_calls += 1
            r = run_tool(client, name, args, env=env, bot_root=bot_root, market_cfg=cfg)
            if isinstance(r, dict) and r.get("source"):
                sources.append(r.get("source"))
            results.append({"tool": name, "result": r})
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": "【工具结果】\n" + json.dumps(results, ensure_ascii=False)
            + "\n\n信息足够请只输出 Plan JSON；否则继续 tool_calls。",
        })
        text = llm.chat_messages(messages)
        rounds += 1
        n_chars += len(messages[-1]["content"]) + len(text)
    return text, {
        "mode": "B_on_demand_tools",
        "latency_s": round(time.time() - t0, 2),
        "prompt_chars": n_chars,
        "snap_bytes": len(json.dumps(snap, ensure_ascii=False)),
        "llm_rounds": rounds,
        "tool_calls": tool_calls,
        "tool_sources": sources,
        "usage": usage_tokens(llm),
    }


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("missing OPENAI_API_KEY")
        return 2
    client, env = make_client()
    symbols = ["ETH_USDT"]
    prompt_text = load_strategy_prompt(ROOT / "prompts" / "eth_range_mid.md")
    llm = LLMClient(LLMConfig(
        model=os.environ.get("LLM_MODEL") or "global:deepseek-v4.1-flash",
        temperature=0.1,
        timeout_sec=90,
        max_tokens=2048,
    ))

    print("=== A: snapshot dump (all TFs into prompt) ===")
    text_a, meta_a = run_mode_a_snapshot(llm, client, env, prompt_text, symbols)
    print("A meta:", json.dumps(meta_a, ensure_ascii=False))
    print("A text:", (text_a or "")[:400].replace("\n", " "))
    try:
        plan_a = parse_plan_text(text_a)
        qa = score_plan(plan_a, text_a)
    except PlanError as e:
        plan_a, qa = None, {"score": 0, "notes": [str(e)]}
    print("A quality:", qa)

    print("\n=== B: on-demand tools ===")
    text_b, meta_b = run_mode_b_tools(llm, client, env, prompt_text, symbols, bot_root=ROOT)
    print("B meta:", json.dumps(meta_b, ensure_ascii=False))
    print("B text:", (text_b or "")[:400].replace("\n", " "))
    try:
        plan_b = parse_plan_text(text_b)
        qb = score_plan(plan_b, text_b)
    except PlanError as e:
        plan_b, qb = None, {"score": 0, "notes": [str(e)]}
    print("B quality:", qb)

    print("\n=== 对比 ===")
    ua, ub = meta_a.get("usage") or {}, meta_b.get("usage") or {}
    print(f"{'指标':<16} {'A快照灌入':>16} {'B工具按需':>16}")
    print(f"{'质量分/5':<16} {qa.get('score'):>16} {qb.get('score'):>16}")
    print(f"{'prompt字符':<16} {meta_a['prompt_chars']:>16} {meta_b['prompt_chars']:>16}")
    print(f"{'快照JSON字节':<16} {meta_a['snap_bytes']:>16} {meta_b['snap_bytes']:>16}")
    print(f"{'LLM轮次':<16} {meta_a['llm_rounds']:>16} {meta_b['llm_rounds']:>16}")
    print(f"{'工具次数':<16} {meta_a['tool_calls']:>16} {meta_b['tool_calls']:>16}")
    print(f"{'token.total':<16} {str(ua.get('total')):>16} {str(ub.get('total')):>16}")
    print(f"{'token.prompt':<16} {str(ua.get('prompt')):>16} {str(ub.get('prompt')):>16}")
    print(f"{'latency_s':<16} {meta_a['latency_s']:>16} {meta_b['latency_s']:>16}")
    print("B tool sources (db=local kline.db, exchange=REST):", meta_b.get("tool_sources"))

    out = ROOT / ".prelaunch_test" / "ab_snapshot_vs_tools.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "A": {"meta": meta_a, "quality": qa, "text": (text_a or "")[:2000]},
        "B": {"meta": meta_b, "quality": qb, "text": (text_b or "")[:2000]},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
