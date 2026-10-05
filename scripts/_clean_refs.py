"""清理 price-action-trading 里的 bot 不可用引用（方案 T8 收尾）。

分两类处理，避免误改知识内容：
1. **精确改**：我生成/保留的 bot 用文件（SKILL.md / 06-rules-index / 07-pitfalls-bans）
   里残留的 `execute_code`、已删除的 `analysis-acceptance.md`、「文末验收表」。
2. **加注不改**：从人类工作流拆出来的知识文件里，成片的「写入 logs/... / 更新 memory/...」
   是原工作流的**记录协议**（描述性表格，不是给 bot 的指令）。给它们加一行说明，
   内容是知识，保留。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "skills-src" / "price-action-trading"
REF = BASE / "references"

# ── 1. 精确替换 ────────────────────────────────────────────────
EXACT: list[tuple[str, str, str]] = [
    ("SKILL.md", "references/knowledge/instrument_crypto_specifics.md",
                 "references/knowledge/instrument_crypto_specifics_part1.md"),
    ("06-rules-index.md", "→ 写入 logs/orders/ → 更新 market_state.md",
                          "→ 把结论写进 chip（region/invalidation/rule_ids/scenarios）"),
    ("07-pitfalls-bans.md",
     "- **对**：主进程 execute_code/脚本加载数据与知识，串行完整分析。",
     "- **对**：用行情工具取数（`klines`/`indicators`），一次取足所需根数，串行完整分析。"),
    ("07-pitfalls-bans.md",
     "- 用 execute_code/脚本一次加载 4H/1H/5M 各 200 根，避免 10+ 次碎调用。",
     "- 一次把 4H/1H/5M 各 200 根取足（`klines` 带 `limit`），避免 10+ 次碎调用。"),
    ("07-pitfalls-bans.md",
     "**正式验收以 [analysis-acceptance.md](analysis-acceptance.md) 为准（G0/G1/G2 含 G1-13～16 + 文末验收表）。**",
     "**验收**：bot 无「文末」，验收并入决策自检 —— 把未通过的规则 ID 写进 `rule_ids` 的说明里。"),
    ("07-pitfalls-bans.md", "- [ ] 文末 **验收表**（深度标签 + PASS/FAIL）", ""),
]

# ── 2. 加注（内容保留）────────────────────────────────────────
NOTE = (
    "> ⚠️ 本文件源自**人类工作流**。其中「写入 `logs/...`、更新 `memory/...`」一类是原工作流的"
    "**记录协议**；bot 没有文件工具，请改为把结论写进 chip 字段"
    "（`region` / `invalidation` / `time_stop_bars` / `give_back_pct` / `risk_pct` / `rule_ids` / `scenarios`），"
    "跨轮记忆由订单上下文与决策日志承担。\n\n"
)
ANNOTATE = [
    "SOUL_part1.md", "SOUL_part2.md",
    "knowledge/theme17_continuity_and_tracking.md",
    "knowledge/workflow_part11.md", "knowledge/workflow_part18.md",
    "knowledge/workflow_part19.md", "knowledge/strategy_workflow_part6.md",
]


def main() -> int:
    changed = 0
    for name, old, new in EXACT:
        p = BASE / name
        if not p.is_file():
            print("  跳过（不存在）", name); continue
        t = p.read_text(encoding="utf-8")
        if old not in t:
            print("  未命中：%-24s %s" % (name, old[:44])); continue
        p.write_text(t.replace(old, new), encoding="utf-8")
        changed += 1
        print("  改：%-24s %s" % (name, old[:44]))

    for name in ANNOTATE:
        p = REF / name
        if not p.is_file():
            print("  跳过（不存在）", name); continue
        t = p.read_text(encoding="utf-8")
        if "本文件源自**人类工作流**" in t:
            print("  已有注：", name); continue
        # 注插在首个标题行之后
        lines = t.splitlines(keepends=True)
        idx = 1 if lines and lines[0].startswith("#") else 0
        lines.insert(idx, "\n" + NOTE)
        p.write_text("".join(lines), encoding="utf-8")
        changed += 1
        print("  加注：", name)

    print("共改动 %d 处" % changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
