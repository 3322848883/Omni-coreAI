"""SkillKit 数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class SkillError(Exception):
    """Skill 加载/校验/调用错误。"""


@dataclass(frozen=True)
class SkillMeta:
    """skill 元数据（L1 catalog 用 name+description）。"""

    id: str
    description: str
    path: Path
    version: Optional[str] = None
    license: Optional[str] = None
    compatibility: Optional[str] = None
    # None = 不收窄；非 None = 激活时仅允许这些工具
    allowed_tools: Optional[frozenset[str]] = None
    model_invocation: bool = True
    risk_level: str = "advisory"
    body_tokens: int = 0

    def catalog_line(self, clip: int = 200) -> str:
        desc = self.description
        if len(desc) > clip:
            desc = desc[: clip - 1] + "…"
        return f"- {self.id}: {desc}"


@dataclass
class SkillPackage:
    """完整 skill 包（meta + body + 捆绑文件）。"""

    meta: SkillMeta
    body: str
    files: dict[str, Path] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.meta.id
