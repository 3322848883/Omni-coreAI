"""一次性拆分脚本：把 price-action-trading 的超限 references 切成 ≤12,000 字符的分片。

背景（实测）：`skill_ref()` 硬切 12,000 字符，超过就被截断。原技能 12 份 theme 文件
（16,685–66,953 字符）与 SOUL.md（17,036 字符）都读不完，等于核心知识不可达。
本脚本按 `##` 小节贪心打包，保证每片 ≤ LIMIT 且**内容一字不丢**。

用法：python scripts/_split_theme.py [--check]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LIMIT = 11500          # 留 500 字符余量（上限 12,000）
BASE = Path(__file__).resolve().parents[1] / "skills-src" / "price-action-trading"
REF = BASE / "references"
KNOW = REF / "knowledge"

HEADER = "> 本文件是 `{src}` 的第 {i}/{n} 片（按 `##` 小节切分，内容未改动）。\n\n"


def _sections(text: str) -> list[tuple[str, str]]:
    """按 `## ` 一级小节切成 (标题, 正文)。前言（无 ## 之前）归到第一片。"""
    parts = re.split(r"(?m)^(?=## )", text)
    out: list[tuple[str, str]] = []
    for p in parts:
        if not p.strip():
            continue
        first = p.splitlines()[0][:60] if p.startswith("## ") else "(前言)"
        out.append((first, p))
    return out


def _refine(text: str, limit: int) -> list[str]:
    """单项超限时逐级细切：`###` → `####` → 空行段落 → 硬切（最后手段）。"""
    for pat in (r"(?m)^(?=### )", r"(?m)^(?=#### )", r"\n\n"):
        subs = [s for s in re.split(pat, text) if s.strip()]
        if len(subs) > 1:
            return _pack_items(subs, limit)
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _pack_items(items: list[str], limit: int) -> list[str]:
    """贪心打包成 ≤limit 的块；单项超限则交给 `_refine` 继续细切。"""
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for it in items:
        if len(it) > limit:
            if cur:
                out.append("".join(cur))
                cur, size = [], 0
            out.extend(_refine(it, limit))
            continue
        if cur and size + len(it) > limit:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(it)
        size += len(it)
    if cur:
        out.append("".join(cur))
    return out


def _pack(sections: list[tuple[str, str]], limit: int) -> list[str]:
    return _pack_items([b for _, b in sections], limit)


def split_file(path: Path, limit: int = LIMIT) -> list[Path]:
    text = path.read_text(encoding="utf-8")
    if len(text) <= limit:
        return [path]
    stem = path.stem
    # 幂等：先清掉上次残留的分片（上次可能中途失败）
    for old in path.parent.glob(f"{stem}_part*.md"):
        old.unlink()
    chunks = _pack(_sections(text), limit)
    made: list[Path] = []
    for i, c in enumerate(chunks, 1):
        body = HEADER.format(src=path.name, i=i, n=len(chunks)) + c
        assert len(body) <= 12000, f"{stem} 第 {i} 片 {len(body)} 字符超限"
        p = path.with_name(f"{stem}_part{i}.md")
        p.write_text(body, encoding="utf-8")
        made.append(p)
    path.unlink()          # 删原文件（内容已在分片里）
    return made


def check() -> int:
    bad = []
    for p in sorted(BASE.rglob("*.md")):
        n = len(p.read_text(encoding="utf-8"))
        if p.name == "SKILL.md":
            if n > 13000:
                bad.append((p, n, 13000))
        elif n > 12000:
            bad.append((p, n, 12000))
    if bad:
        print("超限文件：")
        for p, n, lim in bad:
            print("  %-62s %6d > %d" % (p.relative_to(BASE), n, lim))
        return 1
    print("OK：SKILL.md ≤13,000，其余全部 ≤12,000")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check()

    targets = sorted(KNOW.glob("theme*.md")) + [REF / "SOUL.md"]
    # theme 分片已存在时跳过（幂等）
    for p in targets:
        if not p.is_file():
            print("  跳过（不存在）:", p.name)
            continue
        made = split_file(p)
        if len(made) == 1 and made[0] == p:
            print("  %-44s %6d 字符，无需切" % (p.name, len(p.read_text(encoding='utf-8'))))
        else:
            print("  %-44s -> %d 片: %s" % (p.name, len(made),
                                            ", ".join(m.name for m in made)))
    return check()


if __name__ == "__main__":
    sys.exit(main())
