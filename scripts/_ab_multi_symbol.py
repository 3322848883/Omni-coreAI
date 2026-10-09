# -*- coding: utf-8 -*-
"""多币种提示词 A/B：控制臂（现有人格，单币口吻）vs 多币臂（多币纪律）。

## 设计（变量隔离）

- **档位 = 币种数**：1 = BTC / 2 = +ETH / 3 = +SOL / 5 = +XAU,+XAG
- 两臂**只差 `prompt_file` 一行**（`config/bots/ab-multi-{cur,new}.yaml`），
  币种集合、周期、图数、工具、provider、max_chips 全部逐字相同
- `max_chips` 随币种数放大（否则多币臂被名额截断，两臂不可比）
- **交错跑**：同一档位 cur → new 紧接着（相隔几十秒），把行情漂移压到最小
- **只读**：`analyze_once`（LLM → Plan，不写 inbox、不执行）

## 用法

    python scripts/_ab_multi_symbol.py --levels 1,2      # 先跑两档（4 轮）
    python scripts/_ab_multi_symbol.py --levels 3,5      # 再跑两档（4 轮）

结果落 `C:\\Users\\w6485\\Desktop\\测试\\_ab_multi\\results\\<k>_<arm>.json`，
每份含 prompt 规模 / 图数 / 工具调用（带 symbol）/ token 用量 / Plan 原文 / CoT 尾段。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = Path(r"C:\Users\w6485\Desktop\测试\_ab_multi\results")
FIVE = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XAU_USDT", "XAG_USDT"]
ARMS = {"cur": "ab-multi-cur", "new": "ab-multi-new"}


def load_secrets() -> None:
    """把 scripts/secrets.bat 的 KEY=VALUE 注入环境（只补未设置的）。"""
    p = ROOT / "scripts" / "secrets.bat"
    if not p.is_file():
        print("!! 没有 scripts/secrets.bat，LLM/行情凭据可能缺失")
        return
    txt = p.read_text(encoding="utf-8", errors="replace")
    n = 0
    for line in txt.splitlines():
        m = re.search(r'set\s+"?([A-Za-z_][A-Za-z0-9_]*)=(.*?)"?\s*$', line, re.I)
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip()
        if val and not os.environ.get(name):
            os.environ[name] = val
            n += 1
    print("secrets: 注入 %d 个变量" % n)


def build(k: int, arm: str):
    from omnialpha.__main__ import _build_plan_runner
    from omnialpha.config import load_bot_config
    from omnialpha.watcher import ProjectPaths

    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / ("%s.yaml" % ARMS[arm]))
    s = dict(bot.strategist or {})
    risk = dict(s.get("risk") or {})
    risk["max_chips"] = k                      # 名额 = 币种数（两臂一致）
    s["risk"] = risk
    bot = dataclasses.replace(bot, symbols=FIVE[:k], strategist=s)
    runner = _build_plan_runner(bot, paths)
    return bot, runner


def run_one(k: int, arm: str, repeat: int) -> dict:
    bot, runner = build(k, arm)
    cap: dict = {}

    orig = runner._chat_with_tools

    def patched(system, user, chart_base64=None):
        cap["system"] = system
        cap["user"] = user
        imgs = chart_base64 if isinstance(chart_base64, (list, tuple)) else (
            [chart_base64] if chart_base64 else [])
        cap["charts"] = len(imgs)
        cap["chart_chars"] = sum(len(x or "") for x in imgs)
        cap["llm_calls"] = cap.get("llm_calls", 0) + 1
        text = orig(system, user, chart_base64=chart_base64)
        # 模型原文必须留档：`Chip.to_signal_dict()` 不含 kline_tf/kline_read
        # （那两个字段只作审计落 thinking.json），只看 analyze_once 的返回值
        # 会误判成「模型没写逐K读」。
        cap["text"] = text
        return text

    runner._chat_with_tools = patched
    t0 = time.time()
    err = ""
    res: dict = {}
    try:
        res = runner.analyze_once(trigger="ab-multi") or {}
    except Exception as e:  # noqa: BLE001
        err = repr(e)[:400]
    secs = time.time() - t0

    llm = getattr(runner, "llm", None)
    usage = dict(getattr(llm, "last_usage", None) or {})
    plan = res.get("plan") or {}
    chips = []
    for c in (plan.get("chips") or []):
        if not isinstance(c, dict):
            continue
        chips.append({
            "symbol": c.get("symbol"), "action": c.get("action"),
            "confidence": c.get("confidence"),
            "region": c.get("region"), "kline_tf": c.get("kline_tf"),
            "kline_read_n": len(c.get("kline_read") or []),
            "kline_read": (c.get("kline_read") or [])[:25],
            "tp": c.get("tp"), "tp2": c.get("tp2"), "tp1_share": c.get("tp1_share"),
            "sl": c.get("sl"), "price": c.get("price"),
            "trigger_price": c.get("trigger_price"),
            "size_usd": c.get("size_usd"), "leverage": c.get("leverage"),
            "reasoning": c.get("reasoning"),
            "invalidation": c.get("invalidation"),
            "time_stop_bars": c.get("time_stop_bars"),
        })
    tools = []
    for u in (runner.tool_usage or []):
        a = u.get("args") or {}
        tools.append({
            "tool": u.get("tool"),
            "symbol": a.get("symbol") or a.get("sym") or a.get("symbols"),
            "args": {kk: vv for kk, vv in list(a.items())[:6]},
            "result_len": int(u.get("result_len") or 0),
        })
    rc = [str(x) for x in (getattr(llm, "last_reasoning_chain", None) or [])]

    rec = {
        "k": k, "arm": arm, "repeat": repeat,
        "bot_id": bot.bot_id, "prompt_file": (bot.strategist or {}).get("prompt_file"),
        "symbols": list(bot.symbols), "max_chips": (bot.strategist or {}).get("risk", {}).get("max_chips"),
        "ts": int(time.time()), "elapsed_sec": round(secs, 1), "error": err,
        "ok": res.get("ok"),
        "system_chars": len(cap.get("system") or ""),
        "user_chars": len(cap.get("user") or ""),
        "charts": cap.get("charts", 0), "chart_chars": cap.get("chart_chars", 0),
        "llm_calls": cap.get("llm_calls", 0),
        "usage": usage,
        "model": getattr(llm, "last_model", ""),
        "provider": getattr(llm, "last_provider", ""),
        "finish_reason": getattr(llm, "last_finish_reason", ""),
        "reasoning_segs": [len(x) for x in rc],
        "cot_tail": (rc[-1][-6000:] if rc else ""),
        "plan_reasoning": plan.get("reasoning"),
        "plan_chips": chips,
        "plan_n_chips": len(plan.get("chips") or []),
        "notes": res.get("notes"), "rejected": res.get("rejected"),
        "raw_plan": {kk: vv for kk, vv in plan.items() if kk not in ("chips",)},
        "tools": tools,
        "model_text": cap.get("text") or "",
        "system_head": (cap.get("system") or "")[:400],
        "user_head": (cap.get("user") or "")[:1500],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    fp = OUT / ("k%d_%s_r%d.json" % (k, arm, repeat))
    fp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[k=%d %s r%d] %s  %.0fs  chips=%d  tools=%d  charts=%d  usage=%s  -> %s" % (
        k, arm, repeat, "OK" if not err else "ERR " + err[:60], secs,
        len(chips), len(tools), cap.get("charts", 0),
        {kk: usage.get(kk) for kk in ("prompt_tokens", "completion_tokens", "total_tokens")
         if kk in usage}, fp.name))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,2,3,5")
    ap.add_argument("--arms", default="cur,new")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    load_secrets()
    print("档位 %s / 臂 %s / 每档 %d 轮" % (levels, arms, args.repeat))
    for r in range(1, args.repeat + 1):
        for k in levels:
            for arm in arms:                     # 交错：同档两臂相邻
                run_one(k, arm, r)
    print("完成，结果在 %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
