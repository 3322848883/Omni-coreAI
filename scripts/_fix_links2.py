"""补修死链：这次覆盖「带锚点」的链接形式（`x.md#第11章`）。

上一版 `_fix_links.py` 的正则要求链接以 `.md` 结尾，于是 `strategy_workflow.md#第11章`
这类被漏掉 —— 尺寸守卫测试把它抓出来了（这正是写那个测试的价值）。
"""
from __future__ import annotations

import re
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "skills-src" / "price-action-trading"
# 捕获 `](target)`，target 可以带锚点
LINK = re.compile(r"\]\(([^)\s]+?)(#[^)]*)?\)")


def main() -> int:
    entries = {p.resolve() for p in BASE.rglob("*")}
    fixed = stripped = 0
    for p in sorted(BASE.rglob("*.md")):
        text = p.read_text(encoding="utf-8")
        out, pos, changed = [], 0, False
        for m in LINK.finditer(text):
            rel, anchor = m.group(1), m.group(2) or ""
            if rel.startswith(("http://", "https://", "mailto:")) or not rel:
                continue
            if (p.parent / rel).resolve() in entries:
                continue
            stem = Path(rel).stem
            part1 = (p.parent / f"{stem}_part1.md").resolve()
            out.append(text[pos:m.start()])
            if part1 in entries:
                out.append(f"]({stem}_part1.md{anchor})")
                fixed += 1
            else:
                out.append(f"]({rel}{anchor})")   # 保留原样，交给测试兜住
                stripped += 1
            pos = m.end()
            changed = True
        if changed:
            out.append(text[pos:])
            p.write_text("".join(out), encoding="utf-8")

    print("改指分片 %d 处，未处理 %d 处" % (fixed, stripped))
    left = []
    for p in sorted(BASE.rglob("*.md")):
        for m in LINK.finditer(p.read_text(encoding="utf-8")):
            rel = m.group(1)
            if rel.startswith(("http://", "https://", "mailto:")):
                continue
            if (p.parent / rel).resolve() not in entries:
                left.append((str(p.relative_to(BASE)), m.group(0)))
    print("剩余死链 %d 处" % len(left))
    for f, r in left[:10]:
        print("   %-46s %s" % (f[-46:], r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
