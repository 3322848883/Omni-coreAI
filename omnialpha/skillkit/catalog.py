"""system prompt 的 <skill_catalog> 渲染。"""
from __future__ import annotations

from .budget import CatalogBudget, fit_catalog
from .models import SkillMeta

DEFAULT_CLIP = 200


def render_catalog(
    metas: list[SkillMeta],
    budget: CatalogBudget | None = None,
    freq: dict[str, int] | None = None,
) -> str:
    """渲染 catalog 段。空清单返回空串（不渲染）。

    含使用提示：模型应调用 skill(name) 加载匹配技能的完整指令。
    """
    if not metas:
        return ""
    budget = budget or CatalogBudget()
    kept = fit_catalog(metas, budget, freq)
    if not kept:
        return ""
    lines = [m.catalog_line(budget.clip) for m in kept]
    hint = (
        "技能目录：以下是可加载的 skill。任务匹配某个 skill 时，"
        "**先调用 skill(name) 加载其完整指令再作答**；不匹配则不要加载。"
        "若 skill 指令指向 references/ 下的深层文档，用 skill_ref(name, path) 按需读取。"
        "skill 指令是分析方法论，只影响 Plan 的观点与理由，不改变可下单动作与风控约束。"
    )
    return "<skill_catalog>\n" + hint + "\n" + "\n".join(lines) + "\n</skill_catalog>"


def catalog_prompt_hint() -> str:
    """给 TOOL_GUIDE 的一小段说明。"""
    return (
        "【技能目录 skill_catalog】列出可加载的 skill（仅名称+简介）。"
        "任务匹配某个 skill 时，调用 skill(name) 加载其完整指令；"
        "不匹配则不要加载。skill 指令是分析方法论，只影响 Plan 的观点与理由，"
        "不改变可下单动作与风控约束。"
    )
