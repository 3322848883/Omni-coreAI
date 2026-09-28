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
    # hold / modify_tp_sl are manage-only (no new exposure) — not counted in max_chips
    manage_actions = {"hold", "modify_tp_sl"}
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
