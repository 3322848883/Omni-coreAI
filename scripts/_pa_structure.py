"""pa-analysis：删无用内容 + 拆超限文件（方案 T11/T12）。

先删脚本前已用 `_extract_pa.py` 把「知识而非计算」的部分提取成 4 份 markdown，
本脚本只处理结构。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "skills-src" / "pa-analysis"
ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("st", ROOT / "scripts" / "_split_theme.py")
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)

DEL_DIRS = ["scripts", "agents", "outputs", "assets/guides", "assets/templates"]
DEL_FILES = [
    "references/report-spec.md",        # 产出 HTML 报告用，bot 不产报告
    "references/annotation-spec.md",    # 图面标注 + 渲染工具链，bot 无图
    "references/template-coverage.md",  # 别的系统的 54 板块模板对照
    "references/integration.md",        # 节点1/3/4 契约，不适用
]
SPLIT = [
    "references/plan-schema.md",
    "references/features-spec.md",
    "references/pattern-catalog.md",
    "assets/knowledge/62_bar_counting_pullback.md",
    "references/quality-checklist.md",
]


def main() -> int:
    for d in DEL_DIRS:
        p = BASE / d
        if p.is_dir():
            n = len([f for f in p.rglob("*") if f.is_file()])
            shutil.rmtree(p)
            print("  删目录 %-24s (%d 文件)" % (d, n))
        else:
            print("  跳过（不存在）", d)
    for f in DEL_FILES:
        p = BASE / f
        if p.is_file():
            p.unlink()
            print("  删文件", f)
        else:
            print("  跳过（不存在）", f)

    print("=== 拆分 ===")
    for rel in SPLIT:
        p = BASE / rel
        if not p.is_file():
            print("  跳过（不存在）", rel)
            continue
        n = len(p.read_text(encoding="utf-8"))
        if n <= 12000:
            print("  %-46s %6d 字符，无需切" % (rel, n))
            continue
        made = st.split_file(p, limit=11500)
        print("  %-46s %6d -> %d 片" % (rel, n, len(made)))

    # 尺寸检查
    print("=== 尺寸检查 ===")
    bad = []
    for p in sorted(BASE.rglob("*")):
        if not p.is_file() or p.suffix not in (".md", ".json"):
            continue
        n = len(p.read_text(encoding="utf-8", errors="replace"))
        lim = 13000 if p.name == "SKILL.md" else 12000
        if n > lim:
            bad.append((str(p.relative_to(BASE)), n, lim))
    if bad:
        print("  超限：")
        for f, n, lim in bad:
            print("    %-52s %6d > %d" % (f, n, lim))
    else:
        print("  OK：SKILL.md ≤13,000，其余 ≤12,000")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
