"""skill LLM 工具：定义 + run_tool 实现（L2 按需加载）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .journal import append_journal
from .models import SkillError, SkillPackage
from .registry import SkillRegistry

DEFAULT_MAX_BODY_TOKENS = 5000

SKILL_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": (
            "Load a skill's full instructions by name. Use when the task matches a skill "
            "listed in the skill_catalog. The skill body is returned as instructions for "
            "this turn only. Skills are advisory methodology — they never change order "
            "actions or risk limits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "skill id from the skill_catalog (kebab-case)",
                }
            },
            "required": ["name"],
        },
    },
}

# L3：按需读取 skill 捆绑资源（references/scripts/assets）
SKILL_REF_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skill_ref",
        "description": (
            "Read a bundled file from a skill (references/, scripts/, assets/) after loading "
            "that skill with `skill`. Use for deeper detail the skill body points to. "
            "Read-only; path must stay inside the skill directory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "skill id (kebab-case)"},
                "path": {
                    "type": "string",
                    "description": "relative path inside the skill, e.g. references/SOUL.md",
                },
            },
            "required": ["name", "path"],
        },
    },
}


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


DEFAULT_MAX_REF_TOKENS = 4000


def run_skill_ref(
    registry: SkillRegistry,
    args: dict[str, Any],
    *,
    bot_id: str = "",
    enabled_ids: Optional[list] = None,
    root: Optional[Path] = None,
    max_ref_tokens: int = DEFAULT_MAX_REF_TOKENS,
) -> str:
    """L3：读取 skill 捆绑资源（只读，realpath 必须落在 skill 目录内）。"""
    name = str(args.get("name") or "").strip()
    rel = str(args.get("path") or "").strip()
    if not name or not rel:
        raise SkillError("skill_ref requires 'name' and 'path'")

    pkg = registry.get_package(name)
    if pkg is None:
        avail = ", ".join(registry.ids()) or "(none)"
        raise SkillError(f"unknown skill: {name!r}. available: {avail}")
    if not pkg.meta.model_invocation:
        raise SkillError(f"skill {name!r} is user-invocation-only")
    if enabled_ids is not None and (len(enabled_ids) == 0 or name not in enabled_ids):
        raise SkillError(f"skill {name!r} is not enabled for bot {bot_id or '?'}")

    skill_dir = Path(pkg.meta.path).resolve()
    target = (skill_dir / rel).resolve()
    try:
        target.relative_to(skill_dir)
    except ValueError:
        raise SkillError(f"path escapes skill dir: {rel!r}")

    if not target.is_file():
        raise SkillError(f"file not found in skill {name!r}: {rel}")

    text = target.read_text(encoding="utf-8", errors="replace")
    # 截断判据必须与切点同口径（都按字符）。原先是「token 估算判据 + 字符切点」：
    # `_estimate_tokens` 对中文约 1 token/字，于是 4,000–12,000 字符的中文文件
    # **内容一个字没丢，却被写上 `[reference truncated]`** —— 既误导模型去找并不存在的
    # 「后续内容」，也让 journal 的 truncated 统计失去意义（实测 37 次 skill_ref 里
    # 30 次被标截断，真被切的只有 >12,000 字符的那些）。
    limit_chars = max_ref_tokens * 3
    truncated = len(text) > limit_chars
    if truncated:
        text = text[:limit_chars] + "\n\n[reference truncated]"

    if root is not None:
        append_journal(root, {
            "kind": "skill_ref_read",
            "bot_id": bot_id,
            "skill_id": name,
            "path": rel,
            "truncated": truncated,
        })
    return f"[skill_ref:{name}:{rel}]\n{text}\n[/skill_ref:{name}:{rel}]"


def run_skill_tool(
    registry: SkillRegistry,
    args: dict[str, Any],
    *,
    bot_id: str = "",
    enabled_ids: Optional[list[str]] = None,
    root: Optional[Path] = None,
    max_body_tokens: int = DEFAULT_MAX_BODY_TOKENS,
) -> str:
    """执行 skill 工具。返回 tool result 文本。

    流程：查注册表 → 可见性检查 → model_invocation 检查 →
         读 body（预算截断）→ 包标记返回 → 写 journal。
    """
    name = str(args.get("name") or "").strip()
    if not name:
        raise SkillError("skill tool requires 'name'")

    pkg: Optional[SkillPackage] = registry.get_package(name)
    if pkg is None:
        avail = ", ".join(registry.ids()) or "(none)"
        raise SkillError(f"unknown skill: {name!r}. available: {avail}")

    if not pkg.meta.model_invocation:
        raise SkillError(
            f"skill {name!r} is user-invocation-only "
            f"(model-invocation=false); ask the user to run: omnialpha skill run {name}"
        )

    if enabled_ids is not None:
        if len(enabled_ids) == 0 or name not in enabled_ids:
            raise SkillError(f"skill {name!r} is not enabled for bot {bot_id or '?'}")

    body = pkg.body.strip()
    body_tokens = _estimate_tokens(body)
    truncated = False
    if body_tokens > max_body_tokens:
        # 按字符粗截断
        limit_chars = max_body_tokens * 3
        if len(body) > limit_chars:
            body = body[:limit_chars] + "\n\n[skill body truncated — read references/ for details]"
            truncated = True

    if root is not None:
        append_journal(root, {
            "kind": "skill_activate",
            "bot_id": bot_id,
            "skill_id": name,
            "body_tokens": body_tokens,
            "truncated": truncated,
        })

    return f"[skill:{name}]\n{body}\n[/skill:{name}]"
