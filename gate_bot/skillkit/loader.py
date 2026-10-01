"""Skill 发现与 Safe-YAML frontmatter 解析。

安全约束（不可妥协）：
- 只做 key: value 解析，无代码执行（不用 yaml.load 任意对象构造）
- frontmatter 全文禁 XML 尖括号（会注入 system prompt）
- 引用路径 realpath 必须落在 skill 目录内
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .models import SkillError, SkillMeta, SkillPackage

KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
RESERVED_NAMES = ("gate", "gbot", "gate-bot", "skill", "system", "admin")
SKILL_FILENAME = "SKILL.md"
BUNDLED_DIRS = ("references", "scripts", "assets")
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500


def parse_frontmatter(text: str) -> tuple[Optional[dict[str, str]], str]:
    """解析 YAML frontmatter（Safe 子集：仅 key: value，支持折叠续行）。

    返回 (fields, body)；无 frontmatter 时 (None, 原文)。
    非法结构（含尖括号、无闭合）抛 SkillError。
    """
    if not text.startswith("---"):
        return None, text
    # 找闭合 ---
    rest = text[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    else:
        raise SkillError("frontmatter must start with '---' followed by newline")
    # 空 frontmatter 块：紧接闭合分隔符
    m_empty = re.match(r"^---[ \t]*(\r?\n|$)", rest)
    if m_empty:
        return {}, rest[m_empty.end():]
    end = re.search(r"\n---[ \t]*(\r?\n|$)", rest)
    if not end:
        raise SkillError("frontmatter not closed with '---'")
    fm_block = rest[: end.start()]
    body = rest[end.end():]

    if "<" in fm_block or ">" in fm_block:
        raise SkillError("frontmatter contains XML angle brackets (< >) — forbidden")

    fields: dict[str, str] = {}
    current: Optional[str] = None
    for line in fm_block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            if current:
                fields[current] = (fields[current] + " " + line.strip()).strip()
            continue
        if ":" not in line:
            raise SkillError(f"frontmatter line has no key: {line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            raise SkillError(f"frontmatter empty key: {line!r}")
        val = value.strip()
        # 去掉成对引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        fields[key] = val
        current = key
    return fields, body


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "yes", "1", "on")


def _estimate_tokens(text: str) -> int:
    """粗估 token：中文约 1 字/词，英文约 4 字符/词。取保守上界。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def _fields_to_meta(fields: dict[str, str], path: Path, body: str) -> SkillMeta:
    name = fields.get("name", "").strip()
    desc = fields.get("description", "").strip()
    if not name:
        raise SkillError("frontmatter missing required field 'name'")
    if not KEBAB_RE.match(name):
        raise SkillError(f"name {name!r} is not kebab-case")
    if name in RESERVED_NAMES:
        raise SkillError(f"name {name!r} uses a reserved word")
    if not desc:
        raise SkillError("frontmatter missing required field 'description'")
    if len(desc) > MAX_DESCRIPTION:
        raise SkillError(f"description is {len(desc)} chars (max {MAX_DESCRIPTION})")

    compat = fields.get("compatibility", "").strip() or None
    if compat and len(compat) > MAX_COMPATIBILITY:
        raise SkillError(f"compatibility is {len(compat)} chars (max {MAX_COMPATIBILITY})")

    allowed: Optional[frozenset[str]] = None
    if "allowed-tools" in fields:
        raw = fields["allowed-tools"].strip()
        if raw:
            allowed = frozenset(t for t in raw.split() if t)

    # metadata.gate.* 走 metadata 平铺（loader 只认 metadata.gate.risk_level）
    risk = fields.get("metadata.gate.risk_level", "").strip() or "advisory"

    model_inv = True
    # 兼容两种写法
    for key in ("model-invocation", "disable-model-invocation", "metadata.gate.model_invocation"):
        if key in fields:
            if key == "disable-model-invocation":
                model_inv = not _parse_bool(fields[key])
            else:
                model_inv = _parse_bool(fields[key])

    return SkillMeta(
        id=name,
        description=desc,
        path=path,
        version=fields.get("version", "").strip() or None,
        license=fields.get("license", "").strip() or None,
        compatibility=compat,
        allowed_tools=allowed,
        model_invocation=model_inv,
        risk_level=risk or "advisory",
        body_tokens=_estimate_tokens(body),
    )


def load_package(skill_dir: Path) -> SkillPackage:
    """加载一个 skill 目录为 SkillPackage。"""
    skill_dir = Path(skill_dir).resolve()
    if not skill_dir.is_dir():
        raise SkillError(f"not a directory: {skill_dir}")
    md = skill_dir / SKILL_FILENAME
    if not md.is_file():
        raise SkillError(f"{SKILL_FILENAME} missing in {skill_dir}")
    try:
        text = md.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise SkillError(f"cannot read {SKILL_FILENAME}: {e}") from e
    fields, body = parse_frontmatter(text)
    if fields is None:
        raise SkillError("frontmatter missing: SKILL.md must start with '---' delimited YAML")
    meta = _fields_to_meta(fields, skill_dir, body)

    files: dict[str, Path] = {}
    for sub in BUNDLED_DIRS:
        d = skill_dir / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file():
                rel = p.relative_to(skill_dir).as_posix()
                # realpath 遏制
                if not p.resolve().is_relative_to(skill_dir.resolve()):
                    raise SkillError(f"bundled file escapes skill dir: {rel}")
                files[rel] = p
    return SkillPackage(meta=meta, body=body, files=files)


def scan_skill_dirs(root: Path) -> list[Path]:
    """扫描 root 下的 skill 目录（含一层嵌套命名空间）。

    规则：SKILL.md 所在目录即 skill 目录；发现 SKILL.md 即停向下。
    深度上限 6 层。
    """
    root = Path(root).resolve()
    found: list[Path] = []
    if not root.is_dir():
        return found

    def walk(d: Path, depth: int) -> None:
        if depth > 6:
            return
        if (d / SKILL_FILENAME).is_file():
            found.append(d)
            return
        try:
            children = sorted(c for c in d.iterdir() if c.is_dir() and not c.name.startswith("."))
        except OSError:
            return
        for c in children:
            walk(c, depth + 1)

    walk(root, 0)
    return found


def ensure_kebab_dirname(skill_dir: Path) -> None:
    """目录名必须 kebab-case（validate 用）。"""
    name = Path(skill_dir).name
    if not KEBAB_RE.match(name):
        raise SkillError(f"folder name {name!r} is not kebab-case")
