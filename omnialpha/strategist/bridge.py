"""Plan chips → signal JSON written to inbox/<bot_id>/."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .regime import regime_from_reasoning
from .risk import RiskResult
from .schema import Plan

# 「在铺/加入场单」的动作集合 —— 只有本轮真的在铺新梯/加仓时，才允许用
# `replace=symbol` 让引擎在新单挂完后撤掉该 symbol 的旧梯差集。
# 判据与 executor._INTENT_PRIORITY 的第 5 类（开仓/加仓/突破进场）同源。
# ⚠️ 不含 cancel_*：撤单是显式意图，由引擎另行处理，不需要 replace 代劳。
_ENTRY_ACTIONS = frozenset({
    "open_long", "open_short", "add_long", "add_short",
    "stop_entry_long", "stop_entry_short",
})

# 「显式撤单」动作 —— 与 executor._INTENT_PRIORITY 的第 3 类（撤单）同源。
# ⚠️ 两处必须一致：`tests/test_plan_cancel_needs_entry.py::TestCancelSetInSync` 守这条。
_CANCEL_ACTIONS = frozenset({"cancel_all", "cancel_price_all", "cancel_trail_all"})

# 多空各自的「开新仓」动作 —— regime 闸门按方向拦截。
_BUY_ENTRY = frozenset({"open_long", "add_long", "stop_entry_long"})
_SELL_ENTRY = frozenset({"open_short", "add_short", "stop_entry_short"})


def _apply_regime(accepted: list, regime) -> tuple[list, list[str]]:
    """按模型自报的 `regime` 做硬性收口（plan → signal 的最后一关）。

    规则（全部可选，未写则不动）：
      · `allow_new_long is False`  → 丢弃 open/add/stop_entry_long
      · `allow_new_short is False` → 丢弃 open/add/stop_entry_short
      · `max_layers = N`           → 同方向入场腿最多保留 N 条（按计划里的先后顺序）
    只拦「开新仓」，`close_* / reduce_* / cancel_* / modify_tp_sl` 一律不动 ——
    闸门管的是「要不要新开」，不是「能不能退出」。
    """
    if regime is None:
        return list(accepted), []
    blocked: set[str] = set()
    if getattr(regime, "allow_new_long", None) is False:
        blocked |= _BUY_ENTRY
    if getattr(regime, "allow_new_short", None) is False:
        blocked |= _SELL_ENTRY
    max_layers = getattr(regime, "max_layers", None)
    kept, notes = [], []
    buy_n = sell_n = 0
    for chip in accepted:
        action = str(chip.action or "").lower()
        if action in blocked:
            notes.append(f"regime 拦截 {chip.symbol}/{action}")
            continue
        if action in _BUY_ENTRY:
            if max_layers is not None and buy_n >= max_layers:
                notes.append(f"regime 层数上限 {max_layers}，丢弃 {chip.symbol}/{action}")
                continue
            buy_n += 1
        elif action in _SELL_ENTRY:
            if max_layers is not None and sell_n >= max_layers:
                notes.append(f"regime 层数上限 {max_layers}，丢弃 {chip.symbol}/{action}")
                continue
            sell_n += 1
        kept.append(chip)
    return kept, notes


def _regime_audit(plan: Plan) -> dict | None:
    """**审计通道**：把本轮的方向结论落盘，供事后核对（不参与下单）。

    优先用模型自己输出的 `regime` 块；模型不写时（实测 3/3 都没写），从 `reasoning`
    里抽取（149 份真实计划覆盖 54%、与决策一致 91%）。
    ⚠️ 抽取结果**不做执行依据** —— 覆盖率不到 100%，fail-open 没约束力、fail-closed 会误停。
    执行层只认 `plan.regime`（见 `_apply_regime`）。
    """
    if plan.regime is not None:
        return {**plan.regime.to_dict(), "source": "plan"}
    guess = regime_from_reasoning(plan.reasoning)
    if guess:
        return {**guess, "source": "reasoning"}
    return None


def chips_to_signal(plan: Plan, risk_result: RiskResult, bot_id: str = "") -> dict[str, Any]:
    """Map accepted chips to one SignalFile payload (orders[] + replace)."""
    accepted, regime_notes = _apply_regime(list(risk_result.accepted), plan.regime)
    orders = []
    for chip in accepted:
        action = chip.action
        # hold + tp/sl = move existing protections (was silently dropped)
        if action == "hold":
            if chip.tp is None and chip.sl is None:
                continue
            action = "modify_tp_sl"
        item = chip.to_signal_dict()
        item["action"] = action
        item.setdefault("meta", {})
        item["meta"]["plan_cycle"] = plan.cycle_id
        item["meta"]["confidence"] = chip.confidence
        item["meta"]["chips_reasoning"] = chip.reasoning
        if chip.action == "hold" and action == "modify_tp_sl":
            item["meta"]["from_action"] = "hold"
        orders.append(item)

    # ── 「撤单腿失去承接物」剥离（2026-10-14 网格空窗复发，破坏性半执行）────────
    # LLM 的一轮计划常是「撤旧梯 + 重铺 N 层」= `cancel_all` + `open_long` × N。
    # 风控是**逐 chip** 裁决的，而且低置信度的入场腿不是被丢，而是被**降级成 hold**
    # （`risk.apply_risk`，`drop_on_low_conf=False`）→ 到了这里就是「计划本想铺单、
    # 但通过风控的入场腿一条都没有」。
    # 此时若照旧把 `cancel_all` 发出去，引擎的 `place_before_cancel` 会把它转成
    # 「撤掉快照差集」→ **整梯被撤光且零替补** → 一个轮次节拍的网格空窗。
    # 实测 2026-10-14 16:26:36：LLM `plan_reasoning` = 「旧梯首档距现价仅6点不合规,
    # 重铺10层」，10 条 open_long 因 `confidence 0.0 < 0.55` 全被降级 → 只剩
    # `cancel_all + modify_tp_sl` 落盘 → 10 张阶梯被撤空，直到 16:47:53 才重铺
    # （**空窗 ≈18 分钟**）。破坏性的一半执行了、建设性的一半没有 —— 这是典型的
    # 半执行事故。
    #
    # 判据：**计划里本来有入场腿、但通过风控的入场腿一条都没有** → 撤单腿是在为
    # 「重铺」让位，而重铺没发生 → 剥掉撤单腿。
    # 反例（必须保留撤单）：计划里**本就没有**入场腿（AI 单纯想清空挂单簿）→
    # 撤单就是它的意图本身，照发；计划里的入场腿通过了 → 撤单是为重铺让位，照发
    # （此时 `replace=symbol`，新梯接替旧梯）。
    plan_entry_syms = {
        str(getattr(c, "symbol", "") or "").strip().upper()
        for c in (getattr(plan, "chips", None) or [])
        if str(getattr(c, "action", "") or "").lower() in _ENTRY_ACTIONS
    }
    accepted_entry_syms = {
        str(o.get("symbol") or "").strip().upper()
        for o in orders
        if str(o.get("action") or "").lower() in _ENTRY_ACTIONS
    }
    dropped_cancels: list[dict[str, Any]] = []
    if plan_entry_syms:
        kept: list[dict[str, Any]] = []
        for o in orders:
            act = str(o.get("action") or "").lower()
            if act in _CANCEL_ACTIONS:
                sym = str(o.get("symbol") or "").strip().upper()
                orphaned = (
                    (sym in plan_entry_syms and sym not in accepted_entry_syms)
                    if sym
                    else not (plan_entry_syms & accepted_entry_syms)
                )
                if orphaned:
                    dropped_cancels.append({"symbol": sym or "<all>", "action": act})
                    continue
            kept.append(o)
        orders = kept

    # ── replace 语义收紧（系统级网格空窗修复，2026-10-14）────────────────────
    # 原实现无条件给每个 LLM 计划打上 `replace: symbol`。引擎据此在本轮新单挂完后，
    # 撤掉该 symbol 上「快照里有、新计划里没有」的旧 owned 单。问题是：当本轮计划
    # **只有**改保护 / 减仓 / hold（没有任何新挂入场单）时，「新计划里的单」为空，
    # 整个阶梯被当成陈旧单撤光且无替补 → 一个轮次节拍（实测 15~21min）的网格空窗。
    # 产线实测 10-14：martin-grid 2 天出现 16 次，撤的全是普通挂单（阶梯），
    # reduce-only 的 TP/SL 因保护持仓被保留 —— 盘面即「只剩止盈止损、没有网格」。
    #
    # 收紧为：只有本轮真的在铺/加入场单（_ENTRY_ACTIONS）时才用 replace=symbol
    # （新梯接替旧梯的语义）；否则 none。改保护不依赖 replace —— modify_tp_sl
    # 自身会清理「被替换那一类」的旧保护单（executor._modify_tp_sl）。
    # 这是**引擎契约层**修复，对所有走 LLM 计划的 bot 生效，非单 bot 配置调整。
    has_entry = any(
        str(o.get("action") or "").lower() in _ENTRY_ACTIONS for o in orders
    )
    notes = list(risk_result.notes) + regime_notes
    if dropped_cancels:
        notes.append(
            "剥掉撤单腿（计划里的入场腿一条都没通过风控 → 撤单失去承接物，"
            "照发会变成只撤不挂的网格空窗）: "
            + ", ".join(f"{d['symbol']}/{d['action']}" for d in dropped_cancels)
        )
    payload: dict[str, Any] = {
        "replace": "symbol" if has_entry else "none",
        "orders": orders,
        "meta": {
            "plan_cycle": plan.cycle_id,
            "plan_reasoning": plan.reasoning,
            "kind": "llm_plan",
            "bot_id": bot_id,
            "risk_notes": notes,
            "dropped_cancels": dropped_cancels,
            "regime": _regime_audit(plan),
            "rejected": [
                {"symbol": c.symbol, "action": c.action, "confidence": c.confidence}
                for c in risk_result.rejected
            ],
        },
    }
    return payload


def write_signal_file(inbox: Path, payload: dict[str, Any], cycle_id: str = "") -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = (cycle_id or "plan").replace(":", "").replace("/", "-")[:24]
    path = inbox / f"{ts}-{safe}.json"
    # if orders empty → hold-only audit file (still valid signal: hold)
    if not payload.get("orders"):
        payload = {
            "action": "hold",
            "meta": {
                **(payload.get("meta") or {}),
                "kind": "llm_plan_hold",
            },
        }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_hold_audit(history_dir: Path, plan: Plan, cycle_id: str = "") -> Path:
    """Write hold-only audit when plan produces no orders."""
    history_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = (cycle_id or plan.cycle_id or "hold").replace(":", "").replace("/", "-")[:24]
    path = history_dir / f"{ts}-{safe}.hold.json"
    path.write_text(
        json.dumps(
            {
                "cycle_id": plan.cycle_id or cycle_id,
                "reasoning": plan.reasoning,
                "kind": "llm_plan_hold",
                "chips": [
                    {**c.to_signal_dict(), "confidence": c.confidence} for c in plan.chips
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
