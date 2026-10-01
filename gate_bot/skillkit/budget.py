"""catalog token 预算与溢出降级。"""
from __future__ import annotations

from dataclasses import dataclass

from .models import SkillMeta

DEFAULT_MAX_CATALOG_TOKENS = 2000
DEFAULT_CLIP = 200


@dataclass
class CatalogBudget:
    max_tokens: int = DEFAULT_MAX_CATALOG_TOKENS
    clip: int = DEFAULT_CLIP


def _line_tokens(meta: SkillMeta, clip: int) -> int:
    return len(meta.catalog_line(clip)) // 3 + 5  # 粗估：1 token ≈ 3 字符


def fit_catalog(
    metas: list[SkillMeta],
    budget: CatalogBudget | None = None,
    freq: dict[str, int] | None = None,
) -> list[SkillMeta]:
    """按预算裁剪 catalog 清单。

    溢出降级顺序（对齐 OpenClaw）：
    1. 超出 max_tokens 时，优先保留高频 skill（freq 降序，缺省按 id）
    2. 仍超 → 整条丢弃低频的
    """
    budget = budget or CatalogBudget()
    freq = freq or {}
    # 排序：频次降序，其次 id
    ordered = sorted(metas, key=lambda m: (-freq.get(m.id, 0), m.id))
    kept: list[SkillMeta] = []
    used = 0
    for m in ordered:
        cost = _line_tokens(m, budget.clip)
        if used + cost > budget.max_tokens and kept:
            continue  # 丢弃（降级）
        kept.append(m)
        used += cost
    # 输出按 id 稳定排序
    return sorted(kept, key=lambda m: m.id)


def estimate_catalog_tokens(metas: list[SkillMeta], clip: int = DEFAULT_CLIP) -> int:
    return sum(_line_tokens(m, clip) for m in metas)
