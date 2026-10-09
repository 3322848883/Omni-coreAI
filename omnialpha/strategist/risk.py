"""Risk gate: per-bot strategy risk (confidence / notional / allow / max_chips)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .schema import Chip, Plan


@dataclass
class RiskConfig:
    min_confidence: float = 0.7
    max_notional_usd: Optional[float] = None
    max_chips: int = 5
    # 每个 symbol 的名额（T8/D8）。缺省 1（保守）：名额全局共享时一个币占满名额，
    # 其他币系统性出局（弱币永远轮不到）。
    max_chips_per_symbol: int = 1
    # 品种宇宙（T5-b）。None = 不校验（旧调用方/脚本）；[] = 未配置 → 一律拒绝。
    symbols: Optional[list[str]] = None
    allow_actions: Optional[set[str]] = None  # None = allow all CHIP_ACTIONS
    drop_on_low_conf: bool = False  # True→drop; False→rewrite to hold


@dataclass
class RiskResult:
    accepted: list[Chip] = field(default_factory=list)
    rejected: list[Chip] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _norm_sym(s: Any) -> str:
    return str(s or "").strip().upper()


def _universe(symbols: Any) -> set[str]:
    return {_norm_sym(s) for s in (symbols or []) if _norm_sym(s)}


def _per_symbol_quota_active(risk: RiskConfig, chips: list[Chip]) -> bool:
    """是否进入「按币配额」模式。

    **单币不进入**：缺省 `max_chips_per_symbol=1` 会把单币 bot 的方案砍到 1 条，
    而 `eth-range-paper` / `brooks-pa-paper` 都是单币 + `max_chips: 2` 的实盘配置 ——
    单币行为必须逐字不变（I11）。单币时全局 `max_chips` 就是唯一名额。
    多币时（宇宙 ≥2，或本轮计划里出现 ≥2 个币）每币各拿一份配额。
    """
    if len({_norm_sym(c.symbol) for c in chips}) > 1:
        return True
    return risk.symbols is not None and len(_universe(risk.symbols)) > 1


def apply_risk(plan: Plan, risk: RiskConfig) -> RiskResult:
    out = RiskResult()
    chips = list(plan.chips)
    # ── 宇宙闸门（T5-b）──
    # 必须在名额分配**之前**：宇宙外的 chip 不该抢名额。原先 plan 层完全不校验
    # （只查字符集），越界 chip 静默进 inbox、靠 executor 白名单兜底 → 白烧一轮（B-5）。
    if risk.symbols is not None:
        universe = _universe(risk.symbols)
        kept: list[Chip] = []
        for chip in chips:
            if _norm_sym(chip.symbol) in universe:
                kept.append(chip)
            else:
                out.notes.append(
                    f"reject {chip.symbol} {chip.action}: not in universe "
                    f"({','.join(sorted(universe)) or '未配置'})"
                )
                out.rejected.append(chip)
        chips = kept
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
    action_chips = [c for c in chips if c.action not in manage_actions]
    action_chips.sort(key=lambda c: c.confidence, reverse=True)
    # ── 按币名额（T8/D8）──
    # 原先名额全局共享、跨币按置信度抢 → BTC 占满则其他币系统性出局（B-4）。
    # 改成先按 symbol 分组、各组取 top-N（组序 = 该组最高置信度，故整体仍是置信度序），
    # 全局 `max_chips` 保留为**总上限**。
    if risk.max_chips_per_symbol and _per_symbol_quota_active(risk, action_chips):
        groups: dict[str, list[Chip]] = {}
        for c in action_chips:
            groups.setdefault(c.symbol, []).append(c)
        kept_chips: list[Chip] = []
        for sym, group in groups.items():
            if len(group) > risk.max_chips_per_symbol:
                out.notes.append(
                    f"max_chips_per_symbol: keep top {risk.max_chips_per_symbol}"
                    f"/{len(group)} for {sym}"
                )
                out.rejected.extend(group[risk.max_chips_per_symbol:])
                group = group[: risk.max_chips_per_symbol]
            kept_chips.extend(group)
        action_chips = kept_chips
    if risk.max_chips and len(action_chips) > risk.max_chips:
        action_chips.sort(key=lambda c: c.confidence, reverse=True)
        out.notes.append(f"max_chips: keep top {risk.max_chips}/{len(action_chips)}")
        out.rejected.extend(action_chips[risk.max_chips :])
        action_chips = action_chips[: risk.max_chips]

    rest = [c for c in chips if c.action in manage_actions]
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
