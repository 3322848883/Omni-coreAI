"""Skill 安装期校验（fail-closed）。

ERROR → 拒装（退出码 1）；WARNING → 可装（退出码 0）。
对齐 agentskills.io 与 MiMo validate_skill.py 的约束。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .loader import (
    KEBAB_RE,
    MAX_COMPATIBILITY,
    MAX_DESCRIPTION,
    RESERVED_NAMES,
    SKILL_FILENAME,
    load_package,
    parse_frontmatter,
)
from .models import SkillError

WHEN_CUES = ("use when", "use this", "use for", "trigger", "use it when", "使用场景", "当用户", "用于")
MAX_BODY_WORDS = 5000


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, msg: str) -> None:
        self.errors.append(f"{code} {msg}")

    def warn(self, code: str, msg: str) -> None:
        self.warnings.append(f"{code} {msg}")

    def summary(self) -> str:
        lines = [f"{'PASS' if self.ok else 'FAIL'}  errors={len(self.errors)} warnings={len(self.warnings)}"]
        for e in self.errors:
            lines.append(f"  ERROR {e}")
        for w in self.warnings:
            lines.append(f"  WARN  {w}")
        return "\n".join(lines)


def validate_package(skill_dir: Path) -> ValidationReport:
    """校验一个 skill 目录。返回报告（不抛异常）。"""
    rep = ValidationReport()
    skill_dir = Path(skill_dir)

    if not skill_dir.is_dir():
        rep.error("E00", f"not a directory: {skill_dir}")
        return rep

    folder = skill_dir.name
    if not KEBAB_RE.match(folder):
        rep.error("E01", f"folder name {folder!r} is not kebab-case")

    entries = [e.name for e in skill_dir.iterdir()]
    if SKILL_FILENAME not in entries:
        near = [e for e in entries if e.lower() == "skill.md"]
        if near:
            rep.error("E02", f"found {near[0]!r} — must be named exactly 'SKILL.md' (case-sensitive)")
        else:
            rep.error("E02", "SKILL.md is missing")
        return rep

    if any(e.lower() == "readme.md" for e in entries):
        rep.error("E03", "README.md must not be inside the skill folder")

    md = skill_dir / SKILL_FILENAME
    try:
        text = md.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        rep.error("E04", f"cannot read SKILL.md: {e}")
        return rep

    try:
        fields, body = parse_frontmatter(text)
    except SkillError as e:
        msg = str(e)
        if "angle brackets" in msg:
            rep.error("E05", msg)
        else:
            rep.error("E04", msg)
        return rep

    if fields is None:
        rep.error("E04", "frontmatter missing or malformed")
        return rep

    name = fields.get("name", "")
    desc = fields.get("description", "")
    if not name or not desc:
        rep.error("E06", f"missing required field(s): name={bool(name)} description={bool(desc)}")
    if name:
        if not KEBAB_RE.match(name):
            rep.error("E07", f"name {name!r} is not kebab-case")
        if name in RESERVED_NAMES:
            rep.error("E07", f"name {name!r} uses a reserved word")
        if name != folder:
            rep.warn("W01", f"name {name!r} does not match folder name {folder!r}")
    if desc:
        if len(desc) > MAX_DESCRIPTION:
            rep.error("E08", f"description is {len(desc)} chars (max {MAX_DESCRIPTION})")
        if len(desc) < 40:
            rep.warn("W02", f"description is very short ({len(desc)} chars) — likely too vague")
        if not any(cue in desc.lower() for cue in WHEN_CUES):
            rep.warn("W03", "description has no WHEN clause (e.g. 'Use when ...')")

    compat = fields.get("compatibility", "")
    if compat and not (1 <= len(compat) <= MAX_COMPATIBILITY):
        rep.error("E09", f"compatibility must be 1-{MAX_COMPATIBILITY} chars (got {len(compat)})")

    # body 词数（中文按字计）
    cjk = sum(1 for ch in body if "一" <= ch <= "鿿")
    words = len(body.split()) + cjk
    if words > MAX_BODY_WORDS:
        rep.warn("W04", f"body is ~{words} words (max {MAX_BODY_WORDS}) — move detail to references/")

    # 引用文件存在性 + 路径遏制
    for m in re.finditer(r"(?:scripts|references|assets)/[\w./\-]*\w", body):
        rel = m.group(0)
        target = (skill_dir / rel).resolve()
        try:
            target.relative_to(skill_dir.resolve())
        except ValueError:
            rep.error("E10", f"reference escapes skill dir: {rel}")
            continue
        if not target.exists():
            rep.warn("W05", f"SKILL.md references {rel!r} but it does not exist")

    # 最终整包加载（走 loader 的安全检查）
    if rep.ok:
        try:
            load_package(skill_dir)
        except SkillError as e:
            rep.error("E04", f"load failed: {e}")

    return rep
