"""修分片/删除造成的死链（T7/T8 收尾）。

规则：
- 目标是 `X.md` 但已分片成 `X_part1.md` → 改指 `X_part1.md`
- 目标已按方案删除 → 去掉链接语法，**保留文字**（内容不丢，只是不再是链接）
"""
from __future__ import annotations

import re
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "skills-src" / "price-action-trading"
LINK = re.compile(r"\[([^\]]*)\]\(([^)]+\.md)\)")


def main() -> int:
    files = {p.resolve() for p in BASE.rglob("*") if p.is_file()}
    names = {p.name for p in files}
    fixed_rep = fixed_strip = 0

    for p in sorted(BASE.rglob("*.md")):
        text = p.read_text(encoding="utf-8")
        out: list[str] = []
        pos = 0
        changed = False
        for m in LINK.finditer(text):
            label, rel = m.group(1), m.group(2)
            if rel.startswith(("http://", "https://", "#")):
                continue
            target = (p.parent / rel).resolve()
            if target in files:
                continue
            stem = Path(rel).stem
            part1 = (p.parent / f"{stem}_part1.md").resolve()
            out.append(text[pos:m.start()])
            if part1 in files:
                out.append(f"[{label}]({stem}_part1.md)")
                fixed_rep += 1
            else:
                out.append(label or stem)
                fixed_strip += 1
            pos = m.end()
            changed = True
        if changed:
            out.append(text[pos:])
            p.write_text("".join(out), encoding="utf-8")

    print("改指分片 %d 处，去链接保留文字 %d 处" % (fixed_rep, fixed_strip))

    # 复检
    left = []
    for p in sorted(BASE.rglob("*.md")):
        t = p.read_text(encoding="utf-8")
        for m in LINK.finditer(t):
            rel = m.group(2)
            if rel.startswith(("http://", "https://", "#")):
                continue
            if (p.parent / rel).resolve() not in files:
                left.append((str(p.relative_to(BASE)), rel))
    print("剩余死链：%d 处" % len(left))
    for f, r in left[:10]:
        print("   %-46s -> %s" % (f[-46:], r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
