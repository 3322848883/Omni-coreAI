"""三个人格 · 同一份行情 · 分析质量对比。

为什么必须共用一份快照：三个人格各自跑自己的 bot 配置时，**输入不同**（标的与周期
都不同），那种对比不成立 —— 分不清差异来自人格还是来自行情。

做法：从 `rose-paper`（BTC+ETH、5m 主周期、extra 1m/15m）冻结一份共同快照，
三个人格都吃这一份。副作用是人格1（配置只有 BTC）会看到 ETH 数据 —— 这本身是个
质量观察点：它的人格写着「只做BTC_USDT」，守不守得住。

质量判据**取自各自人格的铁律**，不外加标准：
  1. 有没有先判市场周期，并且用**这个人格自己的周期分类词**
  2. 有没有给出信号判据（人格1 精确信号K线定义 / 人格2 四个形态名 / 人格3 合格信号K线）
  3. 入场方式符不符合这个人格（人格1 止损单 / 人格2 优先限价 / 人格3 止损单触发）
  4. 止损、止盈、仓位是否齐全（都要求按 0.5% 反推）
  5. 有入场时计划几何是否成立（多 sl<entry<tp / 空 tp<entry<sl）
  6. 没机会时有没有说清在等什么
  7. 有没有越过自己的标的范围
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

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.strategist import loop as loop_mod  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402

SNAP_BOT = "rose-paper"          # BTC+ETH、5m 主周期、extra 1m/15m → 覆盖三个人格所需
SNAP = ROOT / "scripts" / "_cmp_snapshot.json"
SKILL = "price-action-trading"

# 「技能配置做全」的那一臂需要三件事：技能可见、契约里有一个**必须外部知识才能填**
# 的字段（这是模型打开技能的唯一动机 —— 实测没有它时 6/6 次 0 读取）、以及
# 「查不到就留空、不许编造」的兜底，避免为了填字段而编造。
# 现状契约里没有 `rule_ids`（我为了消除编造去掉了），所以技能臂要把它加回去。
RULE_IDS_FOR_SKILL_ARM = (
    "`rule_ids`=你依据的规则编号数组，"
    "**必须来自你查到的价格行为学原文条文**；查不到就留空 `[]`，**不许编造**。"
)
_ANCHOR = "`risk_pct`=本单实际风险占权益的**百分数**（`0.4` = 0.4%，不是 0.004）。"

PERSONAS = [
    # key, paper bot, 提示词, 周期分类词, 信号词, 入场要求
    ("人格1-Al Brooks", "brooks-pa-paper", "prompts/brooks_btc_pa.md",
     ["突破", "紧通道", "宽通道", "交易区间", "紧区间"], ["信号K", "H1", "H2", "L1", "L2"],
     "止损单", ["BTC_USDT"]),
    ("人格2-Rose", "rose-paper", "prompts/rose_pa.md",
     ["趋势", "震荡"], ["Caveman", "MTR", "Mother Bar", "ATR Bar"],
     "优先限价", ["BTC_USDT", "ETH_USDT"]),
    ("人格3-方方土", "fangfangtu-paper", "prompts/fangfangtu_pa.md",
     ["突破", "窄通道", "宽通道", "震荡"], ["信号K"],
     "止损单", ["BTC_USDT", "ETH_USDT"]),
]


def freeze_common(refreeze: bool) -> dict:
    if SNAP.is_file() and not refreeze:
        return json.loads(SNAP.read_text(encoding="utf-8"))
    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / f"{SNAP_BOT}.yaml")
    from omnialpha.__main__ import _build_plan_runner
    runner = _build_plan_runner(bot, paths)
    snap = loop_mod.collect_snapshot(
        runner.client, runner.cfg.symbols, candles=runner.cfg.candles,
        interval=runner.cfg.timeframe, market_cfg=runner.cfg.market,
        env=runner.cfg.env, bot_root=runner.cfg.bot_root,
    )
    SNAP.write_text(json.dumps(snap, ensure_ascii=False, default=str), encoding="utf-8")
    print("共同快照 -> %s (%d KB)" % (SNAP.name, SNAP.stat().st_size // 1024))
    return snap


def run_persona(label: str, bot_id: str, prompt_rel: str, frozen: dict,
                skills: list) -> dict:
    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / f"{bot_id}.yaml")
    bot.strategist["skills"] = list(skills)     # 被测变量：能不能用技能
    from omnialpha.__main__ import _build_plan_runner
    runner = _build_plan_runner(bot, paths)

    cap: dict = {}
    orig = runner._chat_with_tools

    def patched(system, user, chart_base64=None):
        cap["system"] = system
        cap["text"] = orig(system, user, chart_base64=chart_base64)
        return cap["text"]

    runner._chat_with_tools = patched
    t0 = time.time()
    res = runner.analyze_once(trigger="cmp-" + bot_id) or {}
    secs = time.time() - t0

    plan = res.get("plan") or {}
    chips = plan.get("chips") or []
    chip = (chips or [{}])[0]
    raw = cap.get("text") or ""
    usage = list(runner.tool_usage or [])
    reads = [u for u in usage if u["tool"] in ("skill", "skill_ref")]
    read_chars = sum(int(u.get("result_len") or 0) for u in reads)

    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    entry = _n(chip.get("price")) or _n(chip.get("trigger_price"))
    sl, tp = _n(chip.get("sl")), _n(chip.get("tp"))
    act = str(chip.get("action") or "")
    geom = None
    if entry and sl and tp:
        geom = (sl < entry < tp) if "long" in act else ((tp < entry < sl) if "short" in act else None)

    return {
        "label": label, "bot": bot_id, "prompt": prompt_rel,
        "skills": list(skills),
        "n_chips": len(chips),
        "read_calls": len(reads), "read_chars": read_chars,
        "read_paths": [str((u.get("args") or {}).get("path") or "") for u in reads
                       if u["tool"] == "skill_ref"],
        "ok": bool(res.get("ok")), "secs": round(secs, 1),
        "decision": plan.get("decision"), "chip": chip, "raw": raw,
        "sys_len": len(cap.get("system") or ""),
        "tools": [u["tool"] for u in (runner.tool_usage or [])],
        "cot_chars": sum(len(s) for s in (getattr(runner.llm, "last_reasoning_chain", []) or [])),
        "entry": entry, "sl": sl, "tp": tp, "tp2": _n(chip.get("tp2")),
        "size_usd": _n(chip.get("size_usd")), "risk_pct": _n(chip.get("risk_pct")),
        "region": chip.get("region"), "geometry": geom,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refreeze", action="store_true")
    # 默认只跑「不用技能」；`--with-skill` 追加对照臂（同一快照、同一人格）
    ap.add_argument("--with-skill", action="store_true")
    args = ap.parse_args()

    frozen = freeze_common(args.refreeze)
    loop_mod.collect_snapshot = lambda *a, **kw: frozen     # 所有臂共用同一份输入
    print("共同快照: %s" % SNAP.name)

    arms = [("无技能", []), ("用技能", [SKILL])] if args.with_skill else [("无技能", [])]
    from omnialpha.strategist import prompt as prompt_mod
    base_prompt = prompt_mod.SYSTEM_PROMPT
    skill_prompt = base_prompt.replace(_ANCHOR, _ANCHOR + RULE_IDS_FOR_SKILL_ARM)
    if args.with_skill and skill_prompt == base_prompt:
        raise SystemExit("契约里找不到锚点，技能臂的 rule_ids 加不进去 —— 先核对 prompt.py")
    results = []
    for label, bot_id, prompt_rel, cycles, signals, entry_rule, allowed in PERSONAS:
        for arm, skills in arms:
            tag = "%s · %s" % (label, arm)
            # 「配置做全」：技能臂的契约里带上 rule_ids（给模型打开技能的理由）
            prompt_mod.SYSTEM_PROMPT = skill_prompt if skills else base_prompt
            print("  跑 %s …" % tag, flush=True)
            r = run_persona(label, bot_id, prompt_rel, frozen, skills)
            r["tag"] = tag
            r["cycles"], r["signals"], r["entry_rule"], r["allowed"] = (
                cycles, signals, entry_rule, allowed)
            # —— 质量判据（全部取自该人格自己的铁律）——
            txt = r["raw"] + " " + json.dumps(r["chip"], ensure_ascii=False)
            r["q_cycle_named"] = any(c in txt for c in cycles)
            r["q_signal_named"] = any(s in txt for s in signals)
            r["q_has_sl"] = r["sl"] is not None
            r["q_has_tp"] = r["tp"] is not None
            r["q_has_size"] = (r["size_usd"] or 0) > 0
            r["q_geometry"] = r["geometry"]
            is_entry = any(k in str(r["decision"] or "") for k in ("open_", "stop_entry_", "add_"))
            r["q_is_entry"] = is_entry
            r["q_entry_style"] = (
                ("限价" if str(r["chip"].get("type")) == "limit" else
                 "止损单" if "stop_entry" in str(r["decision"]) else
                 "市价" if str(r["chip"].get("type")) == "market" else "?")
                if is_entry else "—")
            r["q_out_of_scope"] = sorted({s for s in re.findall(r"\b([A-Z]{2,5}_USDT)\b", txt)
                                          if s not in allowed})
            r["q_wait_explained"] = (not is_entry) and any(w in txt for w in ("等", "等待"))
            # 契约要求的 5 个字段里填了几个
            r["q_fields"] = sum(1 for k in ("region", "invalidation", "time_stop_bars",
                                            "give_back_pct", "risk_pct")
                                if r["chip"].get(k) not in (None, [], {}))
            r["contract_has_rule_ids"] = "rule_ids" in prompt_mod.SYSTEM_PROMPT
            results.append(r)
            print("    %s 秒  决策=%s  读取技能=%d 字符  字段=%d/5  几何=%s" %
                  (r["secs"], r["decision"], r["read_chars"], r["q_fields"], r["geometry"]))

    prompt_mod.SYSTEM_PROMPT = base_prompt        # 复原契约（脚本会按臂改写它）

    # ── 逐人格双臂对照 ──
    rows = [
        ("决策", "decision"),
        ("契约要求 rule_ids", "contract_has_rule_ids"),
        ("读取技能字符", "read_chars"),
        ("skill_ref 次数", "read_calls"),
        ("契约字段填了几个(上限5)", "q_fields"),
        ("先判市场周期(自己的周期词)", "q_cycle_named"),
        ("给出信号判据(自己的信号词)", "q_signal_named"),
        ("是入场计划", "q_is_entry"),
        ("带止损", "q_has_sl"),
        ("带止盈", "q_has_tp"),
        ("带仓位", "q_has_size"),
        ("计划几何成立", "q_geometry"),
        ("无机会时说明在等什么", "q_wait_explained"),
        ("标的越界", "q_out_of_scope"),
        ("耗时秒", "secs"),
        ("CoT 字符", "cot_chars"),
    ]
    for label, *_ in PERSONAS:
        pair = [r for r in results if r["label"] == label]
        print("\n" + "=" * 78)
        print(label)
        print("=" * 78)
        print("%-30s" % "维度" + "".join("%-22s" % r["tag"].split(" · ")[-1] for r in pair))
        for name, key in rows:
            line = "%-30s" % name
            for r in pair:
                v = r.get(key)
                line += "%-22s" % ("" if v is None else str(v))
            print(line)

    out = ROOT / "scripts" / "_cmp_personas.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ROOT / "scripts" / "_cmp_personas.md"
    with md.open("w", encoding="utf-8") as f:
        f.write("# 三个人格 · 同一份行情 · 分析质量对比\n\n")
        f.write("共同快照：`%s`（冻结自 `%s`）\n\n" % (SNAP.name, SNAP_BOT))
        for r in results:
            f.write("\n---\n\n## %s（`%s`）\n\n" % (r["tag"], r["bot"]))
            f.write("提示词 `%s`　决策 `%s`　耗时 %ss　工具 %d 个　"
                    "读取技能 %d 字符（%d 次）\n\n"
                    % (r["prompt"], r["decision"], r["secs"], len(r["tools"]),
                       r["read_chars"], r["read_calls"]))
            if r["read_paths"]:
                f.write("读了：`%s`\n\n" % r["read_paths"])
            f.write("工具序列：`%s`\n\n" % r["tools"])
            f.write("chip：\n```json\n%s\n```\n\n" % json.dumps(r["chip"], ensure_ascii=False, indent=1))
            f.write("### 原始输出\n\n```\n%s\n```\n" % r["raw"])
    print("\n明细 -> %s\n原始输出 -> %s" % (out.name, md.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
