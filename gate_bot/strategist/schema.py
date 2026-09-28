"""Plan / chips schema for LLM strategist (multi-symbol decisions)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

CHIP_ACTIONS = {
    "open_long",
    "open_short",
    "add_long",
    "add_short",
    "reduce_long",
    "reduce_short",
    "close",
    "close_all",
    "hold",
    "stop_entry_long",
    "stop_entry_short",
    "flatten",
    "cancel_all",
    "cancel_price_all",
    "modify_tp_sl",
}

CHIP_ORDER_TYPES = {"market", "limit", "post_only", "ioc", "fok"}


class PlanError(Exception):
    pass


def _safe_symbol(symbol: str) -> str:
    import re
    from ..gate_client import resolve_symbol
    s = resolve_symbol(str(symbol or ''))
    if not re.fullmatch(r'[A-Z0-9_]{2,20}', s or ''):
        raise PlanError(f'symbol invalid: {symbol!r}')
    return s
    pass


@dataclass
class Chip:
    symbol: str
    action: str
    confidence: float = 0.0
    size_usd: Optional[float] = None
    size: Optional[int] = None
    tp: Optional[float] = None
    sl: Optional[float] = None
    tp2: Optional[float] = None  # 多级止盈
    tp3: Optional[float] = None
    tp1_share: Optional[float] = None
    tp2_share: Optional[float] = None
    order_type: str = "market"
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    leverage: Optional[int] = None
    side: Optional[str] = None  # long|short — dual-position manage (close/modify_tp_sl)
    tp_mode: str = "trigger"  # trigger | limit_order
    sl_mode: str = "trigger"
    reasoning: str = ""

    def to_signal_dict(self) -> dict:
        d: dict[str, Any] = {"action": self.action, "symbol": self.symbol, "type": self.order_type}
        if self.size is not None:
            d["size"] = self.size
        if self.size_usd is not None:
            d["size_usd"] = self.size_usd
        if self.price is not None:
            d["price"] = self.price
        if self.tp is not None:
            d["tp"] = self.tp
        if self.sl is not None:
            d["sl"] = self.sl
        if self.tp2 is not None:
            d["tp2"] = self.tp2
        if self.tp3 is not None:
            d["tp3"] = self.tp3
        if self.tp1_share is not None:
            d["tp1_share"] = self.tp1_share
        if self.tp2_share is not None:
            d["tp2_share"] = self.tp2_share
        if self.trigger_price is not None:
            d["trigger_price"] = self.trigger_price
        if self.leverage is not None:
            d["leverage"] = self.leverage
        if self.side:
            d["side"] = self.side
        if self.tp_mode and self.tp_mode != "trigger":
            d["tp_mode"] = self.tp_mode
        if self.sl_mode and self.sl_mode != "trigger":
            d["sl_mode"] = self.sl_mode
        if self.reasoning:
            d.setdefault("meta", {})["reasoning"] = self.reasoning
        return d


@dataclass
class Plan:
    cycle_id: str
    reasoning: str = ""
    chips: list[Chip] = field(default_factory=list)
    triggers: list = field(default_factory=list)
    trigger_ops: list = field(default_factory=list)
    rejected_triggers: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    return float(v)


def parse_plan(data: Any) -> Plan:
    if not isinstance(data, dict):
        raise PlanError("plan must be a JSON object")
    chips_raw = data.get("chips") or []
    if not isinstance(chips_raw, list):
        raise PlanError("chips must be an array")
    if len(chips_raw) > 50:
        raise PlanError("chips too large (max 50)")
    chips: list[Chip] = []
    for i, raw in enumerate(chips_raw):
        if not isinstance(raw, dict):
            raise PlanError(f"chips[{i}] must be object")
        action = str(raw.get("action") or "").strip().lower()
        order_type_raw = str(raw.get("type") or "market").strip().lower()
        # recovery: model sometimes puts action name in `type` (e.g. type=stop_entry_long)
        if order_type_raw not in CHIP_ORDER_TYPES and order_type_raw in CHIP_ACTIONS:
            if not action or action == "hold":
                action = order_type_raw
            order_type_raw = "market"
        if action not in CHIP_ACTIONS:
            raise PlanError(f"chips[{i}].action unsupported: {action!r}")
        symbol = str(raw.get("symbol") or "").strip()
        if not symbol:
            raise PlanError(f"chips[{i}].symbol required")
        conf = float(raw.get("confidence") or 0)
        if not 0 <= conf <= 1:
            if 0 < conf <= 100:
                conf = conf / 100.0
            else:
                raise PlanError(f"chips[{i}].confidence out of range")
        order_type = order_type_raw
        if order_type not in CHIP_ORDER_TYPES:
            raise PlanError(f"chips[{i}].type unsupported: {order_type!r}")
        side = raw.get("side")
        if side is not None:
            side = str(side).strip().lower()
            if side not in ("long", "short"):
                raise PlanError(f"chips[{i}].side must be long|short, got {raw.get('side')!r}")
        tp_mode = str(raw.get("tp_mode") or "trigger").lower()
        sl_mode = str(raw.get("sl_mode") or "trigger").lower()
        for name, m in (("tp_mode", tp_mode), ("sl_mode", sl_mode)):
            if m not in ("trigger", "limit_order", "limit"):
                raise PlanError(f"chips[{i}].{name} unsupported: {m!r}")
        chips.append(
            Chip(
                symbol=_safe_symbol(symbol),
                action=action,
                confidence=conf,
                size_usd=_f(raw.get("size_usd")),
                size=int(raw["size"]) if raw.get("size") is not None else None,
                tp=_f(raw.get("tp")),
                sl=_f(raw.get("sl")),
                tp2=_f(raw.get("tp2")),
                tp3=_f(raw.get("tp3")),
                tp1_share=_f(raw.get("tp1_share")),
                tp2_share=_f(raw.get("tp2_share")),
                order_type=order_type,
                price=_f(raw.get("price")),
                trigger_price=_f(raw.get("trigger_price")),
                leverage=int(raw["leverage"]) if raw.get("leverage") is not None else None,
                side=side,
                tp_mode="limit_order" if tp_mode == "limit" else tp_mode,
                sl_mode="limit_order" if sl_mode == "limit" else sl_mode,
                reasoning=str(raw.get("reasoning") or ""),
            )
        )
    triggers = data.get("triggers") or []
    if not isinstance(triggers, list):
        raise PlanError("triggers must be an array")
    trigger_ops = data.get("trigger_ops") or []
    if not isinstance(trigger_ops, list):
        raise PlanError("trigger_ops must be an array")
    return Plan(
        cycle_id=str(data.get("cycle_id") or ""),
        reasoning=str(data.get("reasoning") or ""),
        chips=chips,
        triggers=triggers,
        trigger_ops=trigger_ops,
        raw=data,
    )


def _extract_json_object(text: str):
    """Extract the first balanced JSON object from LLM output."""
    import json

    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        raise PlanError("LLM output has no JSON object")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start : i + 1])
                except json.JSONDecodeError as e:
                    raise PlanError(f"LLM output is not valid JSON: {e}") from e
    raise PlanError("LLM output JSON object is truncated/unterminated")


def parse_plan_text(text: str) -> Plan:
    import json

    data = _extract_json_object(text)
    return parse_plan(data)
