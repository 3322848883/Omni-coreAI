"""技能尺寸守卫：把「bot 一次能读完」这件事变成可回归的约束。

**为什么要有这个测试**：技能的「读不完」不是一次性的问题，而是会随内容增长悄悄回归的
问题。实测教训 —— `skill_ref()` 硬切 12,000 字符，而原技能 `workflow.md` 有 151,551 字符
（只能读到 7.9%），核心 theme1-12 全部读不到；全历史 544 次 `skill_activate` 只换来
37 次 `skill_ref_read`，其中 30 次还被截断。所以上限必须被测试钉住。

口径（实测，非推断）：
- `skill()` 返回 SKILL.md 正文，硬切 15,000 字符 → 设计上限 13,000（留余量）
- `skill_ref()` 返回单个文件，硬切 12,000 字符 → 设计上限 12,000
- L1 catalog 的 `catalog_line(clip=200)` 会把 description 截到 200 字符 → 上限 200
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.skillkit.loader import parse_frontmatter  # noqa: E402

SKILLS_SRC = ROOT / "skills-src"

SKILL_BODY_LIMIT = 13000     # skill() 硬切 15,000
REF_LIMIT = 12000            # skill_ref() 硬切 12,000
DESC_LIMIT = 200             # L1 catalog clip

LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _skill_dirs() -> list[Path]:
    if not SKILLS_SRC.is_dir():
        return []
    return sorted(d for d in SKILLS_SRC.iterdir() if (d / "SKILL.md").is_file())


class TestSkillSizes(unittest.TestCase):
    def setUp(self):
        self.skills = _skill_dirs()
        if not self.skills:
            self.skipTest("skills-src 下没有已安装的技能")

    def test_skill_body_within_limit(self):
        for d in self.skills:
            with self.subTest(skill=d.name):
                _, body = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
                self.assertLessEqual(
                    len(body), SKILL_BODY_LIMIT,
                    f"{d.name}/SKILL.md 正文 {len(body)} 字符 > {SKILL_BODY_LIMIT}"
                    f"（skill() 硬切 15,000，超了就读不全）")

    def test_description_within_catalog_clip(self):
        for d in self.skills:
            with self.subTest(skill=d.name):
                fields, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
                desc = str(fields.get("description") or "")
                self.assertLessEqual(
                    len(desc), DESC_LIMIT,
                    f"{d.name} 的 description {len(desc)} 字符 > {DESC_LIMIT}"
                    f"（L1 catalog clip=200，超了尾部会被截掉）")
                self.assertIn("Use when", desc, f"{d.name} 的 description 缺 WHEN 子句")

    def test_every_file_within_ref_limit(self):
        over = []
        for d in self.skills:
            for p in d.rglob("*"):
                if not p.is_file() or p.suffix not in (".md", ".json", ".txt", ".yaml", ".yml"):
                    continue
                if p.name == "SKILL.md":
                    continue
                n = len(p.read_text(encoding="utf-8", errors="replace"))
                if n > REF_LIMIT:
                    over.append((f"{d.name}/{p.relative_to(d)}", n))
        self.assertEqual(
            over, [],
            "以下文件超过 skill_ref 的 12,000 字符上限，bot 读不全：\n"
            + "\n".join(f"  {f}: {n}" for f, n in over))

    def test_no_dead_links(self):
        bad = []
        for d in self.skills:
            # 文件与目录都算有效目标（`../assets/examples` 这类目录链接是合法的）
            entries = {p.resolve() for p in d.rglob("*")}
            for p in d.rglob("*.md"):
                for m in LINK.finditer(p.read_text(encoding="utf-8")):
                    rel = m.group(2).split("#")[0].strip()   # 去掉锚点再判
                    if not rel or rel.startswith(("http://", "https://", "mailto:")):
                        continue
                    if (p.parent / rel).resolve() not in entries:
                        bad.append(f"{d.name}/{p.relative_to(d)} -> {m.group(2)}")
        self.assertEqual(bad, [], "死链：\n" + "\n".join(f"  {b}" for b in bad))

    def test_no_bot_unusable_instructions(self):
        """技能里不得留 bot 做不到的指令（无代码执行 / 无文件写入 / 不产 HTML）。"""
        pats = ("execute_code", "python scripts", "脚本加载")
        bad = []
        for d in self.skills:
            for p in d.rglob("*.md"):
                t = p.read_text(encoding="utf-8")
                for pat in pats:
                    if pat in t:
                        bad.append(f"{d.name}/{p.relative_to(d)}: {pat}")
        self.assertEqual(bad, [], "残留 bot 不可用指令：\n" + "\n".join(f"  {b}" for b in bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
