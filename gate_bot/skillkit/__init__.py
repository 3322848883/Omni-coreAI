"""SkillKit — 可安装 skill 引擎（发现 → 校验 → catalog → 按需加载）。

红线：skill 一律 advisory，只影响 Plan JSON 观点；执行闸门永远在 executor + yaml 风控。
"""
from .models import SkillMeta, SkillPackage, SkillError
from .loader import load_package, parse_frontmatter, scan_skill_dirs
from .validate import validate_package, ValidationReport
from .registry import SkillRegistry
from .budget import fit_catalog
from .catalog import render_catalog
from .tool import SKILL_TOOL_DEF, SKILL_REF_TOOL_DEF, run_skill_tool, run_skill_ref

__all__ = [
    "SkillMeta", "SkillPackage", "SkillError",
    "load_package", "parse_frontmatter", "scan_skill_dirs",
    "validate_package", "ValidationReport",
    "SkillRegistry", "fit_catalog", "render_catalog",
    "SKILL_TOOL_DEF", "SKILL_REF_TOOL_DEF", "run_skill_tool", "run_skill_ref",
]
