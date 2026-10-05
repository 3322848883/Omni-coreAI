"""清掉 analysis-acceptance / 文末验收表 的残留引用（该文件已按方案删除）。

用脚本文件而不是 `python -c`：反引号在 PowerShell 传参时会被吃掉。
"""
from __future__ import annotations

from pathlib import Path

REF = Path(__file__).resolve().parents[1] / "skills-src" / "price-action-trading" / "references"

PAIRS: list[tuple[str, str, str]] = [
    ("mindmap-gap-list.md",
     "| `references/analysis-acceptance.md` | **G1-13** 周期四选一+混合；**G1-14** 20 根法则；**G1-15** 四技能自检；**G1-16** MM/目标类型 |",
     "| 决策自检（写进 `rule_ids`） | **G1-13** 周期四选一+混合；**G1-14** 20 根法则；**G1-15** 四技能自检；**G1-16** MM/目标类型 |"),
    ("SOUL_part1.md",
     "| `references/analysis-acceptance.md` | **分析验收规范**：G0 硬性 / G1 深度（含 G1-13～16）/ G2 闭环；文末验收表强制 |",
     "| 决策自检 | G0 硬性 / G1 深度（含 G1-13～16）/ G2 闭环 —— bot 无「文末」，改由 chip 字段承载 |"),
    ("SOUL_part2.md",
     "- **不可省略文末验收表** — 按 `references/analysis-acceptance.md` 勾选 G0/G1/G2；无验收表 = 未验收，完整分析不得视为合格交付",
     "- **不可省略决策自检** — 勾选 G0/G1/G2 语义；bot 无「文末」，把未通过项写进 `rule_ids` 说明"),
]


def main() -> int:
    for name, old, new in PAIRS:
        p = REF / name
        if not p.is_file():
            print("  跳过（不存在）", name)
            continue
        t = p.read_text(encoding="utf-8")
        if old not in t:
            print("  未命中：", name, "|", old[:60])
            continue
        p.write_text(t.replace(old, new), encoding="utf-8")
        print("  改：", name)

    base = REF.parent
    left: dict[str, list[str]] = {}
    for p in base.rglob("*.md"):
        t = p.read_text(encoding="utf-8")
        for pat in ("execute_code", "python scripts", "analysis-acceptance",
                    "文末验收表", "验收表"):
            if pat in t:
                left.setdefault(pat, []).append(str(p.relative_to(base)))
    print()
    if left:
        print("残留：")
        for k, v in left.items():
            print("  %-20s %d 处：%s" % (k, len(v), ", ".join(v[:5])))
    else:
        print("残留：无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
