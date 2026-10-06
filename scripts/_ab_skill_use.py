"""对照：**同一份提示词、同一份冻结行情**，只切「能不能用技能」。

A（不用技能）：`strategist.skills = []` → L1 catalog 为空，模型无从加载知识库
B（用技能）  ：`strategist.skills = [price-action-trading]` → 提示词要求首轮强制 skill_ref

为什么必须冻结行情：拿「这一轮」和「下一轮」比是不成立的——行情不同。
真正可控的对照要让输入完全一致，只让被测变量不同（与 `_ab_test.py` 同一套做法）。

**跑在模拟盘 bot 上**（`*-paper`）：`analyze_once` 会把 CoT 写进
`state/*.thinking.json`，用实盘 bot 会污染正在运行的实盘审计痕迹。
三个 persona 的 paper bot 引用的是同一份提示词文件，结论不受影响。

测什么（都是可机械判定的）：
  1. **知识加载是否真的发生**：`skill` / `skill_ref` 调用次数、读到的总字符数。
     旧行为是 0 次 skill_ref、2MB 里只读 6.9KB（0.34%）。
  2. **知识是否进入结论**：`rule_ids` 是否非空，且**引用的 ID 是否真实存在**
     （拿技能正文与规则索引里出现过的 ID 做白名单；编造 ID 是明确的劣质信号）。
  3. **契约字段填充率**：7 个 Tier 1 字段。
  4. **可执行性**：多单 sl<entry<tp / 空单 tp<entry<sl；`region=range` 时不得有 tp2。
  5. **成本**：CoT 字符、工具调用数、耗时。

用法：
  python scripts/_ab_skill_use.py --persona brooks   [--reps 2] [--refreeze]
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

SKILL = "price-action-trading"

# persona → (paper bot, 提示词文件)
PERSONAS = {
    "brooks":     ("brooks-pa-paper",   "prompts/brooks_btc_pa.md"),
    "fangfangtu": ("fangfangtu-paper",  "prompts/fangfangtu_pa.md"),
    "rose":       ("rose-paper",        "prompts/rose_pa.md"),
}

# 契约**要求**填的字段（prompt.py 第 18 条）。两个曾经的 Tier-1 字段已不在其中：
#   - `scenarios`：契约明确「**不要写**：没有消费方，嵌套结构只会增加解析失败风险」
#   - `rule_ids`：它是技能体系的产物，人格不用技能时无源可引 → 契约一旦要求就会
#     被编造（实测先编假名字、改措辞后改塞裸数字），已从契约中移除
TIER1 = ["region", "invalidation", "time_stop_bars", "give_back_pct", "risk_pct"]

# 技能里真实存在的规则 ID：单字母前缀也算（`R-001` / `T-001` / `V-000`）。
# **`{2,4}` 会漏掉单字母族**（踩过：R-001/T-001 被误判成编造）。
ID_RE = re.compile(r"\b([A-Z]{1,4})-(\d{2,3})\b")
# 技能把规则族写成**区间**（`R-001~R-015`、`T-001~T-012`），字面只出现端点 ——
# 不展开的话区间内部的 ID 会被误判成编造。
RANGE_RE = re.compile(r"\b([A-Z]{1,4})-(\d{2,3})\s*[~～-]\s*([A-Z]{1,4})-(\d{2,3})\b")


def valid_rule_ids() -> set[str]:
    """从技能正文与规则索引里抽出**真实存在**的规则 ID 集合（含区间展开）。"""
    out: set[str] = set()
    for rel in ("SKILL.md", "references/06-rules-index.md", "references/00-core-steps.md",
                "references/SOUL.md", "references/07-pitfalls-bans.md"):
        p = ROOT / "skills-src" / SKILL / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        for a, b in ID_RE.findall(text):
            out.add(f"{a}-{b}")
        for pa, na, pb, nb in RANGE_RE.findall(text):
            if pa != pb:
                continue
            for n in range(int(na), int(nb) + 1):
                out.add(f"{pa}-{n:0{len(na)}d}")
    return out


def snapshot_path(persona: str) -> Path:
    return ROOT / "scripts" / f"_ab_snap_{persona}.json"


def freeze(bot_id: str, persona: str) -> dict:
    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / f"{bot_id}.yaml")
    from omnialpha.__main__ import _build_plan_runner
    runner = _build_plan_runner(bot, paths)
    snap = loop_mod.collect_snapshot(
        runner.client, runner.cfg.symbols, candles=runner.cfg.candles,
        interval=runner.cfg.timeframe, market_cfg=runner.cfg.market,
        env=runner.cfg.env, bot_root=runner.cfg.bot_root,
    )
    p = snapshot_path(persona)
    p.write_text(json.dumps(snap, ensure_ascii=False, default=str), encoding="utf-8")
    print("冻结快照 -> %s (%d KB)" % (p.name, p.stat().st_size // 1024))
    return snap


def run_one(bot_id: str, frozen: dict, skills: list, tag: str, valid: set[str]) -> dict:
    paths = ProjectPaths(ROOT)
    bot = load_bot_config(paths.config_dir / f"{bot_id}.yaml")
    # 被测变量：能不能用技能
    bot.strategist["skills"] = list(skills)
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
    res = runner.analyze_once(trigger=f"abskill-{tag}") or {}
    secs = time.time() - t0

    usage = list(runner.tool_usage or [])
    reads = [u for u in usage if u["tool"] in ("skill", "skill_ref")]
    read_chars = sum(int(u.get("result_len") or 0) for u in reads)
    ref_calls = [u for u in usage if u["tool"] == "skill_ref"]
    # 被拒的加载（技能不在 catalog 时 `skill()`/`skill_ref()` 会抛错，返回的是错误文本）
    read_errors = sum(1 for u in reads
                      if any(m in (u.get("result_preview") or "")
                             for m in ("Error", "error", "not enabled", "不存在", "失败")))
    # **必须读真实的 catalog 段**：不能用「system prompt 里有没有技能名」判断 ——
    # 提示词本身就会写 `skill(name="price-action-trading")`，那个检查恒为真（踩过）。
    catalog = runner._skill_catalog()
    catalog_has_skill = SKILL in catalog

    plan = res.get("plan") or {}
    chip = (plan.get("chips") or [{}])[0]
    raw = cap.get("text") or ""

    # rule_ids：chip 里的 + 原始输出里出现的，都算「模型引用了」
    chip_ids = [str(x) for x in (chip.get("rule_ids") or [])]
    raw_ids = sorted({f"{a}-{b}" for a, b in ID_RE.findall(raw)})
    cited = sorted(set(chip_ids) | set(raw_ids))
    fabricated = sorted(x for x in cited if x not in valid)

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    entry = _num(chip.get("price")) or _num(chip.get("trigger_price"))
    sl, tp = _num(chip.get("sl")), _num(chip.get("tp"))
    side = str(chip.get("action") or "")
    geometry = None
    if entry and sl and tp:
        if "long" in side:
            geometry = (sl < entry < tp)
        elif "short" in side:
            geometry = (tp < entry < sl)

    region = chip.get("region")
    tp2 = chip.get("tp2")

    return {
        "tag": tag,
        "ok": bool(res.get("ok")),
        "skills_visible": skills,
        "catalog_has_skill": catalog_has_skill,
        "catalog_len": len(catalog),
        "read_calls": len(reads),
        "ref_calls": len(ref_calls),
        "read_chars": read_chars,
        "read_errors": read_errors,
        "read_paths": [str((u.get("args") or {}).get("path") or "") for u in ref_calls],
        "n_tools": len(usage),
        "tools": [u["tool"] for u in usage],
        "secs": round(secs, 1),
        "cot_chars": sum(len(s) for s in (getattr(runner.llm, "last_reasoning_chain", []) or [])),
        "fields_filled": [f for f in TIER1 if chip.get(f) not in (None, [], {})],
        # 契约已不要求、模型仍写出来即为**多余产出**（`rule_ids` 现属此类）
        "wrote_rule_ids": chip.get("rule_ids") not in (None, [], {}),
        # 契约明确**禁止**写 scenarios（无消费方 + 嵌套结构易致解析失败）
        "wrote_scenarios": chip.get("scenarios") not in (None, {}, []),
        "rule_ids_cited": cited,
        "rule_ids_fabricated": fabricated,
        "decision": plan.get("decision"),
        "chip": chip,
        "geometry_ok": geometry,
        "range_has_tp2": (region == "range" and tp2 not in (None, 0, "", "0")),
        "raw": raw,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True, choices=sorted(PERSONAS))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--refreeze", action="store_true")
    # 默认**只跑「不用技能」**：技能只是参考工具，人格自带全部判据（用户决定）。
    # 加 `--with-skill` 才跑对照臂，用于将来复核「技能可见是否有正向效果」。
    ap.add_argument("--with-skill", action="store_true")
    args = ap.parse_args()

    bot_id, prompt_rel = PERSONAS[args.persona]
    print("=" * 78)
    print("persona=%s  bot=%s  prompt=%s" % (args.persona, bot_id, prompt_rel))
    print("=" * 78)

    p = snapshot_path(args.persona)
    frozen = (freeze(bot_id, args.persona) if args.refreeze or not p.is_file()
              else json.loads(p.read_text(encoding="utf-8")))
    print("快照: %s" % p.name)
    loop_mod.collect_snapshot = lambda *a, **kw: frozen   # 所有对照共用同一份输入

    valid = valid_rule_ids()
    print("技能里真实规则 ID 共 %d 个（用于甄别编造）" % len(valid))

    arms = (("A-不用技能", []),)
    if args.with_skill:
        arms = arms + (("B-用技能", [SKILL]),)
    results: list[dict] = []
    for name, skills in arms:
        for rep in range(1, args.reps + 1):
            tag = f"{name}-{rep}"
            print("  跑 %s …" % tag, flush=True)
            r = run_one(bot_id, frozen, skills, tag, valid)
            results.append(r)
            print("    ok=%s skill_ref=%d 读取=%d 字符 工具=%d CoT=%d 耗时=%ss decision=%s"
                  % (r["ok"], r["ref_calls"], r["read_chars"], r["n_tools"],
                     r["cot_chars"], r["secs"], r["decision"]))

    def avg(key, prefix):
        vals = [r[key] for r in results if r["tag"].startswith(prefix)]
        return (sum(vals) / len(vals)) if vals else 0

    print("\n" + "=" * 78)
    print("对照有效性（先看这段，再看结论）")
    print("=" * 78)
    for r in results:
        print("  %-18s catalog含技能=%-6s catalog长度=%-5d 加载被拒=%d 实际读取=%d"
              % (r["tag"], r["catalog_has_skill"], r["catalog_len"],
                 r["read_errors"], r["read_chars"]))

    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    print("%-24s %-14s %-14s" % ("指标", "A-不用技能", "B-用技能"))
    for key, label in (("read_calls", "知识加载调用数"), ("ref_calls", "skill_ref 次数"),
                       ("read_chars", "实际读取字符"), ("n_tools", "工具调用数"),
                       ("cot_chars", "CoT 字符"), ("secs", "耗时秒")):
        print("%-24s %-14.1f %-14.1f" % (label, avg(key, "A-"), avg(key, "B-")))

    for key, label in (("fields_filled", "Tier1 字段填充"), ("rule_ids_cited", "引用的规则 ID"),
                       ("rule_ids_fabricated", "其中编造的")):
        a = [r[key] for r in results if r["tag"].startswith("A-")]
        b = [r[key] for r in results if r["tag"].startswith("B-")]
        print("%-24s A=%s  B=%s" % (label, [len(x) for x in a], [len(x) for x in b]))

    print("\n%-24s %-14s %-14s" % ("逐次明细", "A", "B"))
    for key in ("decision", "geometry_ok", "range_has_tp2",
                "wrote_rule_ids", "wrote_scenarios"):
        a = [r[key] for r in results if r["tag"].startswith("A-")]
        b = [r[key] for r in results if r["tag"].startswith("B-")]
        print("%-24s %-14s %-14s" % (key, a, b))

    out = ROOT / "scripts" / f"_ab_skill_use_{args.persona}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    # 原始输出留档，供人工判质量
    md = ROOT / "scripts" / f"_ab_skill_use_{args.persona}.md"
    with md.open("w", encoding="utf-8") as f:
        f.write(f"# {args.persona} · 用技能 vs 不用技能 · 原始输出\n\n")
        f.write(f"prompt: `{prompt_rel}`　bot: `{bot_id}`　快照: `{p.name}`\n\n")
        for r in results:
            f.write(f"\n---\n\n## {r['tag']}\n\n")
            f.write(f"- skill_ref 次数: {r['ref_calls']}　读取字符: {r['read_chars']}\n")
            f.write(f"- 读的文件: {r['read_paths']}\n")
            f.write(f"- 工具: {r['tools']}\n")
            f.write(f"- Tier1 已填: {r['fields_filled']}\n")
            f.write(f"- 引用规则 ID: {r['rule_ids_cited']}（编造: {r['rule_ids_fabricated']}）\n")
            f.write(f"- 决策: {r['decision']}　chip: `{json.dumps(r['chip'], ensure_ascii=False)}`\n")
            f.write(f"\n### 原始输出\n\n```json\n{r['raw']}\n```\n")
    print("\n明细 -> %s\n原始输出 -> %s" % (out.name, md.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
