"""Risk gate: per-bot strategy risk (confidence / notional / allow / max_chips)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .schema import Chip, Plan


@dataclass
class RiskConfig:
    min_confidence: float = 0.7
    max_notional_usd: Optional[float] = None
    max_chips: int = 5
    allow_actions: Optional[set[str]] = None  # None = allow all CHIP_ACTIONS
    drop_on_low_conf: bool = False  # True→drop; False→rewrite to hold


@dataclass
class RiskResult:
    accepted: list[Chip] = field(default_factory=list)
    rejected: list[Chip] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def apply_risk(plan: Plan, risk: RiskConfig) -> RiskResult:
    out = RiskResult()
    # manage-only（不产生新敞口）——**不占 max_chips 名额**。
    #
    # `cancel_*` 必须在内：它只撤挂单/条件单，敞口只减不增。原先它被当成开仓类，
    # 与 `open_*` / `stop_entry_*` 抢同一个名额并**按置信度排序**——而模型对「这单
    # 该撤」的置信度天然更高（孤儿单场景 0.90~0.95，开仓只有 0.60~0.62），于是
    # **撤单系统性挤掉开仓**。
    #
    # 实测（2026-09-30 ~ 10-08 全部 726 个归档计划）：70 轮（9.6%）被 max_chips 截断，
    # 其中被拒的是开仓类 **67 次（93%）**，而抢到名额的 `cancel_*` 有 **67 次（96%）**，
    # 开仓只赢了 3 次。后果是「撤旧 + 挂新」的两步决策永远只完成撤单那一步，下一轮
    # 模型看到「无挂单」只好再决策一次 —— 8 天 67 次错失开仓 + 循环空转。
    #
    # 不含 `close_*` / `reduce_*`：它们虽也减敞口，但同轮出现「平 + 开」是换仓，
    # 值得仍受名额约束；且 726 个计划里这两类从未被 max_chips 拒过，不动它们。
    manage_actions = {"hold", "modify_tp_sl", "cancel_all", "cancel_price_all"}
    action_chips = [c for c in plan.chips if c.action not in manage_actions]
    action_chips.sort(key=lambda c: c.confidence, reverse=True)
    if risk.max_chips and len(action_chips) > risk.max_chips:
        out.notes.append(f"max_chips: keep top {risk.max_chips}/{len(action_chips)}")
        out.rejected.extend(action_chips[risk.max_chips :])
        action_chips = action_chips[: risk.max_chips]

    rest = [c for c in plan.chips if c.action in manage_actions]
    for chip in action_chips + rest:
        if chip.action == "modify_tp_sl":
            if risk.allow_actions is not None and chip.action not in risk.allow_actions:
                out.notes.append(f"reject {chip.symbol} {chip.action}: not in allow_actions")
                out.rejected.append(chip)
                continue
            if chip.tp is None and chip.sl is None:
                out.notes.append(f"reject {chip.symbol} modify_tp_sl: missing tp/sl")
                out.rejected.append(chip)
                continue
            out.accepted.append(chip)
            continue
        if chip.action != "hold":
            if risk.allow_actions is not None and chip.action not in risk.allow_actions:
                out.notes.append(f"reject {chip.symbol} {chip.action}: not in allow_actions")
                out.rejected.append(chip)
                continue
            if chip.confidence < risk.min_confidence:
                out.notes.append(
                    f"low confidence {chip.symbol} {chip.confidence} < {risk.min_confidence}"
                )
                if risk.drop_on_low_conf:
                    out.rejected.append(chip)
                    continue
                chip = Chip(
                    symbol=chip.symbol,
                    action="hold",
                    confidence=chip.confidence,
                    reasoning=chip.reasoning or "low confidence → hold",
                )
            if risk.max_notional_usd is not None and chip.size_usd is not None:
                if chip.size_usd > risk.max_notional_usd:
                    # spec default: reject oversized notional (do not silently truncate)
                    out.notes.append(
                        f"reject {chip.symbol} size_usd {chip.size_usd} > max {risk.max_notional_usd}"
                    )
                    out.rejected.append(chip)
                    continue
            if chip.size_usd is None and chip.size is None and chip.action.startswith(("open", "add", "stop_entry")):
                out.notes.append(f"reject {chip.symbol} {chip.action}: missing size")
                out.rejected.append(chip)
                continue
        out.accepted.append(chip)
    return out
