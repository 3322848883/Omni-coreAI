# -*- coding: utf-8 -*-
"""单币种「换标的」A/B：把 ETH 换进来，其余不变，看分析质量会不会掉。

## 三格（同一时间窗，按顺序跑）

| 配置 | 标的 | 人格里的币名 |
|---|---|---|
| `ab-swap-btc-cur` | BTC_USDT | BTC（现有人格 `brooks_btc_pa.md`） |
| `ab-swap-eth-cur` | ETH_USDT | **BTC（现有人格，未改）** ← 换标的但人格还写着 BTC |
| `ab-swap-eth-eth` | ETH_USDT | ETH（只改币名 `brooks_eth_pa.md`） |

问的问题：**人格里写死的 BTC 痕迹，会不会让「换标的」后的分析答错标的**？
判据全是机械的：chip 的 symbol 对不对、价位落在哪个币的真实区间、
逐K读的读数属于哪个币、输出文本里还提不提 BTC。

只读：`analyze_once`（不写 inbox、不下单），bot 为 `env: paper`。

用法: python scripts/_ab_swap.py            # 跑三格
      python scripts/_ab_swap.py --analyze  # 只看已有结果
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts._ab_multi_symbol import load_secrets  # noqa: E402

OUT = Path(r"C:\Users\w6485\Desktop\测试\_ab_swap\results")
CASES = [
    ("btc-cur", "ab-swap-btc-cur", "BTC_USDT", "prompts/brooks_btc_pa.md"),
    ("eth-cur", "ab-swap-eth-cur", "ETH_USDT", "prompts/brooks_btc_pa.md"),
    ("eth-eth", "ab-swap-eth-eth", "ETH_USDT", "prompts/brooks_eth_pa.md"),
]


def run_case(tag: str, bot_id: str, rep: int = 1) -> dict:
    from omnialpha.__main__ import _build_plan_runner
    from omnialpha.config import load_bot_config
    from omnialpha.watcher import ProjectPaths

    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / ("%s.yaml" % bot_id))
    runner = _build_plan_runner(bot, paths)
    cap: dict = {}
    orig = runner._chat_with_tools

    def patched(system, user, chart_base64=None):
        cap["system"], cap["user"] = system, user
        imgs = chart_base64 if isinstance(chart_base64, (list, tuple)) else (
            [chart_base64] if chart_base64 else [])
        cap["charts"] = len(imgs)
        cap["llm_calls"] = cap.get("llm_calls", 0) + 1
        text = orig(system, user, chart_base64=chart_base64)
        cap["text"] = text
        return text

    runner._chat_with_tools = patched
    t0 = time.time()
    err, res = "", {}
    try:
        res = runner.analyze_once(trigger="ab-swap") or {}
    except Exception as e:  # noqa: BLE001
        err = repr(e)[:400]
    secs = time.time() - t0

    llm = getattr(runner, "llm", None)
    plan = res.get("plan") or {}
    rec = {
        "tag": tag, "rep": rep, "bot_id": bot_id, "symbols": list(bot.symbols),
        "prompt_file": (bot.strategist or {}).get("prompt_file"),
        "ts": int(time.time()), "elapsed_sec": round(secs, 1), "error": err,
        "ok": res.get("ok"),
        "charts": cap.get("charts", 0), "llm_calls": cap.get("llm_calls", 0),
        "usage": dict(getattr(llm, "last_usage", None) or {}),
        "plan_reasoning": plan.get("reasoning"),
        "plan_chips": plan.get("chips") or [],
        "model_text": cap.get("text") or "",
        "cot_tail": ((list(getattr(llm, "last_reasoning_chain", None) or []) or [""])[-1])[-5000:],
        "tools": [{"tool": u.get("tool"),
                   "symbol": (u.get("args") or {}).get("symbol")
                             or (u.get("args") or {}).get("sym"),
                   "result_len": int(u.get("result_len") or 0)}
                  for u in (runner.tool_usage or [])],
        "system_head": (cap.get("system") or "")[:300],
        "user_head": (cap.get("user") or "")[:800],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ("%s_r%d.json" % (tag, rep))).write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    u = rec["usage"]
    print("[%-8s r%d] %-9s %-28s %s  %.0fs  chips=%d  tok=%s  charts=%d  plan=%r" % (
        tag, rep, ",".join(bot.symbols), rec["prompt_file"],
        "OK" if not err else "ERR " + err[:60], secs, len(rec["plan_chips"]),
        u.get("total_tokens"), rec["charts"], str(rec["plan_reasoning"])[:40]))
    return rec


def analyze() -> int:
    bands = json.loads(
        (Path(r"C:\Users\w6485\Desktop\测试\_ab_multi\results") / "_metrics.json")
        .read_text(encoding="utf-8"))["bands"]
    eth = bands["ETH_USDT"]
    btc = bands["BTC_USDT"]
    print("\n真实区间：ETH last=%.2f [%.2f, %.2f] ｜ BTC last=%.1f [%.1f, %.1f]" % (
        eth["last"], eth["lo"], eth["hi"], btc["last"], btc["lo"], btc["hi"]))

    def nums(text):
        for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(text)):
            try:
                v = float(m.group(0).replace(",", ""))
            except ValueError:
                continue
            if v < 20 or v >= 1_000_000:      # 去小整数与 epoch
                continue
            yield v

    def inband(v, b, pad=0.1):
        return b["lo"] * (1 - pad) <= v <= b["hi"] * (1 + pad)

    rows = []
    for tag, _bot, want_sym, want_prompt in CASES:
        for p in sorted(OUT.glob("%s_r*.json" % tag)):
            r = json.loads(p.read_text(encoding="utf-8"))
            chips = r["plan_chips"]
            syms = [c.get("symbol") for c in chips]
            wrong_sym = [s for s in syms if s != want_sym]
            degraded = "[降级]" in str(r.get("plan_reasoning") or "")
            px_eth = px_btc = 0
            for c in chips:
                for k in ("tp", "tp2", "sl", "price", "trigger_price"):
                    v = c.get(k)
                    if v in (None, "", 0):
                        continue
                    if inband(float(v), eth):
                        px_eth += 1
                    elif inband(float(v), btc):
                        px_btc += 1
            txt = r.get("model_text") or ""
            try:
                rawchips = json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip(),
                                            flags=re.M)).get("chips") or []
            except Exception:  # noqa: BLE001
                rawchips = []
            read_eth = read_btc = 0
            for c in rawchips:
                for line in (c.get("kline_read") or []):
                    for v in nums(line):
                        if inband(v, eth):
                            read_eth += 1
                        elif inband(v, btc):
                            read_btc += 1
            rows.append({
                "tag": tag, "rep": r.get("rep"), "want": want_sym, "prompt": want_prompt,
                "n_chips": len(chips), "chip_syms": syms, "wrong_sym": wrong_sym,
                "degraded": degraded, "json_ok": bool(rawchips),
                "px_eth": px_eth, "px_btc": px_btc,
                "read_eth": read_eth, "read_btc": read_btc,
                "mentions_btc": len(re.findall(r"BTC", txt)),
                "mentions_eth": len(re.findall(r"ETH", txt)),
                "tok": (r.get("usage") or {}).get("total_tokens"),
                "secs": r["elapsed_sec"], "err": r.get("error"),
                "action": (chips[0].get("action") if chips else None),
            })

    print("\n== 逐轮明细 ==")
    print("%-8s %-4s %-9s %-30s %-6s %-12s %-7s %-9s %-12s %-9s" % (
        "格", "轮", "标的", "人格", "chips", "chip symbol", "降级", "JSON可解",
        "价位E/B", "读E/B"))
    for x in rows:
        print("%-8s r%-3s %-9s %-30s %-6d %-12s %-7s %-9s %-12s %-9s" % (
            x["tag"], x["rep"], x["want"], x["prompt"].replace("prompts/", ""),
            x["n_chips"], ",".join(s.split("_")[0] for s in x["chip_syms"]),
            "是" if x["degraded"] else "否", "是" if x["json_ok"] else "否",
            "%d/%d" % (x["px_eth"], x["px_btc"]), "%d/%d" % (x["read_eth"], x["read_btc"])))

    print("\n== 按格汇总 ==")
    for tag, _bot, want_sym, want_prompt in CASES:
        g = [x for x in rows if x["tag"] == tag]
        if not g:
            continue
        print("  %-8s n=%d  标的错误=%d  降级=%d  JSON不可解=%d  平均 token=%.0f  平均耗时=%.0fs" % (
            tag, len(g), sum(1 for x in g if x["wrong_sym"]),
            sum(1 for x in g if x["degraded"]), sum(1 for x in g if not x["json_ok"]),
            sum(x["tok"] or 0 for x in g) / len(g), sum(x["secs"] for x in g) / len(g)))
    bad = [x for x in rows if x["wrong_sym"] or x["err"]]
    print("\n标的错误/异常:", bad if bad else "无")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "_metrics.json").write_text(
        json.dumps({"bands": {"ETH": eth, "BTC": btc}, "rows": rows},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    if not args.analyze:
        load_secrets()
        for rep in range(1, args.repeat + 1):
            for tag, bot_id, _sym, _prompt in CASES:
                run_case(tag, bot_id, rep)
    return analyze()


if __name__ == "__main__":
    raise SystemExit(main())
