"""Plan chips → signal JSON written to inbox/<bot_id>/."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .risk import RiskResult
from .schema import Plan


def chips_to_signal(plan: Plan, risk_result: RiskResult, bot_id: str = "") -> dict[str, Any]:
    """Map accepted chips to one SignalFile payload (orders[] + replace)."""
    orders = []
    for chip in risk_result.accepted:
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

    payload: dict[str, Any] = {
        "replace": "symbol",
        "orders": orders,
        "meta": {
            "plan_cycle": plan.cycle_id,
            "plan_reasoning": plan.reasoning,
            "kind": "llm_plan",
            "bot_id": bot_id,
            "risk_notes": risk_result.notes,
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
