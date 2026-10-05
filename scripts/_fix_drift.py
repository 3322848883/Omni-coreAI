"""T9 收尾：统一版本声明 + 修死链。

只改「对当前版本的声明」，不改「历史来源标注」（如「第24章 v33 新增」——
那是知识出处，属于内容）。
"""
from __future__ import annotations

from pathlib import Path

REF = Path(__file__).resolve().parents[1] / "skills-src" / "price-action-trading" / "references"

PAIRS: list[tuple[str, str, str]] = [
    ("SOUL_part1.md",
     "| `SKILL.md` | v34.2 技能入口：触发说明 + 能力 + 路径规范 + 索引 + 执行摘要 |",
     "| `SKILL.md` | v35.0 技能入口：输出契约 + 每轮入口清单 + 规则索引 + references 路由 |"),
    ("07-pitfalls-bans.md", "# Skill Pitfalls（v34 合并版）", "# 陷阱与禁止清单（v35.0 合并版）"),
    ("07-pitfalls-bans.md", "详见 [pitfalls-v32.md](pitfalls-v32.md)。",
                            "（v32 时代的旧 pitfalls 已并入本文件。）"),
    ("knowledge/workflow_part1.md",
     "# Al Brooks 价格行为交易系统 -- 完整工作流 v30",
     "# Al Brooks 价格行为交易系统 -- 完整工作流（v35.0 技能内）"),
    ("knowledge/workflow_part18.md", "- **版本**：v24.0", "- **版本**：v35.0"),
]


def main() -> int:
    for name, old, new in PAIRS:
        p = REF / name
        if not p.is_file():
            print("  跳过（不存在）", name)
            continue
        t = p.read_text(encoding="utf-8")
        if old not in t:
            print("  未命中：", name, "|", old[:56])
            continue
        p.write_text(t.replace(old, new), encoding="utf-8")
        print("  改：", name, "|", old[:56])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
