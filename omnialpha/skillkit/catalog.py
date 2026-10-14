"""system prompt 的 <skill_catalog> 渲染。"""
from __future__ import annotations

from .budget import CatalogBudget, fit_catalog
from .models import SkillMeta

DEFAULT_CLIP = 200


def render_catalog(
    metas: list[SkillMeta],
    budget: CatalogBudget | None = None,
    freq: dict[str, int] | None = None,
    required: bool = False,
) -> str:
    """渲染 catalog 段。空清单返回空串（不渲染）。

    含使用提示：模型应调用 skill(name) 加载匹配技能的完整指令。

    required=True：**必载模式**。bot 明确声明「这些 skill 就是本 bot 的决策方法论」，
    hint 从「任务匹配时加载」升级为「每轮决策前必须先加载」。背景实测（2026-10-12
    brooks-eth 审计）：按需语义下 152 轮只有 20 轮主动加载（13%），开仓轮覆盖率
    3/23（13%）——模型对「本任务是否匹配」的判断过于保守。必载模式只在 bot 显式
    配置 `skills_required: true` 时生效，默认关闭、行为与改动前一致。
    """
    if not metas:
        return ""
    budget = budget or CatalogBudget()
    kept = fit_catalog(metas, budget, freq)
    if not kept:
        return ""
    lines = [m.catalog_line(budget.clip) for m in kept]
    if required:
        names = ", ".join(m.id for m in kept)
        hint = (
            f"技能目录（必载）：本 bot 的决策方法论技能为 [{names}]。"
            "**每轮决策必须先调用 skill(name) 加载其完整指令，走完其中的强制清单"
            "（含 HC 硬约束核对）后再输出 Plan；未加载技能就输出 Plan 视为流程违规。**"
            "若 skill 指令指向 references/ 下的深层文档，用 skill_ref(name, path) 按需读取。"
            "skill 指令是分析方法论，只影响 Plan 的观点与理由，不改变可下单动作与风控约束。"
        )
    else:
        hint = (
            "技能目录：以下是可加载的 skill。任务匹配某个 skill 时，"
            "**先调用 skill(name) 加载其完整指令再作答**；不匹配则不要加载。"
            "若 skill 指令指向 references/ 下的深层文档，用 skill_ref(name, path) 按需读取。"
            "skill 指令是分析方法论，只影响 Plan 的观点与理由，不改变可下单动作与风控约束。"
        )
    return "<skill_catalog>\n" + hint + "\n" + "\n".join(lines) + "\n</skill_catalog>"

