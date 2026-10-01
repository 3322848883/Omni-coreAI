"""SkillRegistry：扫描、优先级、每 bot 可见集。"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from .loader import load_package, scan_skill_dirs
from .models import SkillError, SkillMeta, SkillPackage


class SkillRegistry:
    """skill 注册表。

    扫描根优先级：先给出的根优先（项目级 > 全局级）。
    同名 skill 取高优先级根。
    """

    def __init__(self) -> None:
        self._packages: dict[str, SkillPackage] = {}
        self._roots: list[Path] = []

    # ── 扫描 ─────────────────────────────────────
    def scan(self, roots: Iterable[Path]) -> list[SkillMeta]:
        self._roots = [Path(r) for r in roots]
        self._packages.clear()
        found: list[SkillMeta] = []
        for root in self._roots:
            for d in scan_skill_dirs(root):
                try:
                    pkg = load_package(d)
                except (SkillError, UnicodeDecodeError, OSError, ValueError):
                    continue  # 坏包/编码错误/权限问题不进注册表（validate 会报）
                if pkg.meta.id in self._packages:
                    continue  # 同名取先扫到的（高优先级根）
                self._packages[pkg.meta.id] = pkg
                found.append(pkg.meta)
        return found

    # ── 查询 ─────────────────────────────────────
    def all_metas(self) -> list[SkillMeta]:
        return [p.meta for p in self._packages.values()]

    def visible_for(self, bot_id: str, enabled_ids: Optional[list[str]] = None) -> list[SkillMeta]:
        """bot 可见 skill。

        enabled_ids：
          - None → 默认可见全部 model_invocation=True 的 skill
          - []   → 不可见任何 skill
          - [..] → 仅这些 id（且须 model_invocation=True）
        """
        if enabled_ids is not None and len(enabled_ids) == 0:
            return []
        out: list[SkillMeta] = []
        for pkg in self._packages.values():
            if not pkg.meta.model_invocation:
                continue
            if enabled_ids is not None and pkg.meta.id not in enabled_ids:
                continue
            out.append(pkg.meta)
        return sorted(out, key=lambda m: m.id)

    def get_meta(self, skill_id: str) -> Optional[SkillMeta]:
        pkg = self._packages.get(skill_id)
        return pkg.meta if pkg else None

    def get_package(self, skill_id: str) -> Optional[SkillPackage]:
        return self._packages.get(skill_id)

    def load_body(self, skill_id: str) -> str:
        pkg = self._packages.get(skill_id)
        if pkg is None:
            raise SkillError(f"unknown skill: {skill_id}")
        return pkg.body

    def ids(self) -> list[str]:
        return sorted(self._packages.keys())
