"""Signal JSON schema: parse, validate, expand grid / multi-order payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .gate_client import resolve_symbol

ACTIONS = {
    "open_long",
    "open_short",
    "close",
    "close_all",
    "cancel_all",
    "cancel_price_all",
    "cancel_trail_all",
    "hold",
    "grid",
    "trail",
}

ORDER_TYPES = {"market", "limit", "post_only", "ioc", "fok"}
PRICE_TYPES = {"latest", "mark", "index"}


class SchemaError(Exception):
    pass


@dataclass
class Intent:
    action: str
    symbol: str = ""
    size_usd: Optional[float] = None
    size_pct: Optional[float] = None
    margin_pct: Optional[float] = None
    size: Optional[int] = None
    order_type: str = "market"
    price: Optional[float] = None
    leverage: Optional[int] = None
    tp: Optional[float] = None
    sl: Optional[float] = None
    tp_type: str = "limit"
    sl_type: str = "limit"
    tp_limit_price: Optional[float] = None
    sl_limit_price: Optional[float] = None
    trigger_price_type: str = "latest"
    trigger_rule_tp: Optional[int] = None
    trigger_rule_sl: Optional[int] = None
    trigger_expiration: Optional[int] = None
    margin_mode: Optional[str] = None
    side: Optional[str] = None  # for close / grid
    close_size: Optional[int] = None
    price_offset: Optional[str] = None
    activation_price: Optional[str] = None
    label: str = "signal"
    meta: dict = field(default_factory=dict)

    @property
    def needs_open(self) -> bool:
        return self.action in ("open_long", "open_short")


@dataclass
class SignalFile:
    intents: list[Intent]
    meta: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def _f(value: Any, name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise SchemaError(f"{name} must be a number, got {value!r}") from e


def _i(value: Any, name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise SchemaError(f"{name} must be an integer, got {value!r}") from e


def infer_trigger_rules(action: str, is_tp: bool) -> int:
    """1 = price >= trigger, 2 = price <= trigger."""
    if action in ("open_long", "grid"):
        return 1 if is_tp else 2
    if action == "open_short":
        return 2 if is_tp else 1
    return 1 if is_tp else 2


def parse_intent(data: dict, default_label: str = "signal") -> Intent:
    if not isinstance(data, dict):
        raise SchemaError("intent must be an object")
    action = str(data.get("action") or "").strip().lower()
    # empty / missing action is a legal no-op (hold)
    if not action:
        return Intent(action="hold", meta=data.get("meta") or {}, label=default_label)
    if action not in ACTIONS:
        raise SchemaError(f"unsupported action: {action!r}")

    if action in ("hold",):
        return Intent(action="hold", meta=data.get("meta") or {}, label=default_label)

    if action in ("close_all", "cancel_all", "cancel_price_all", "cancel_trail_all"):
        symbol = data.get("symbol") or ""
        return Intent(
            action=action,
            symbol=resolve_symbol(symbol) if symbol else "",
            side=data.get("side"),
            label=str(data.get("label") or default_label),
            meta=data.get("meta") or {},
        )

    if action == "trail":
        symbol = data.get("symbol")
        if not symbol:
            raise SchemaError("trail requires symbol")
        amount = _i(data.get("amount") or data.get("size"), "amount")
        if amount is None or amount == 0:
            raise SchemaError("trail requires non-zero amount (positive=buy, negative=sell)")
        offset = data.get("price_offset")
        if offset is None or offset == "":
            raise SchemaError("trail requires price_offset (e.g. 0.5 or 0.5%)")
        return Intent(
            action="trail",
            symbol=resolve_symbol(symbol),
            size=abs(int(amount)),
            side="long" if amount > 0 else "short",
            price_offset=str(offset),
            activation_price=str(data.get("activation_price") or "0"),
            label=str(data.get("label") or default_label),
            meta=data.get("meta") or {},
        )

    if action == "grid":
        return _parse_grid(data, default_label)

    if action == "close":
        symbol = data.get("symbol")
        if not symbol:
            raise SchemaError("close requires symbol")
        side = data.get("side")
        if side is not None:
            side = str(side).lower()
            if side not in ("long", "short"):
                raise SchemaError("close.side must be long|short")
        size = _i(data.get("size"), "size")
        return Intent(
            action="close",
            symbol=resolve_symbol(symbol),
            side=side,
            close_size=size,
            label=str(data.get("label") or default_label),
            meta=data.get("meta") or {},
        )

    # open_long / open_short
    symbol = data.get("symbol")
    if not symbol:
        raise SchemaError(f"{action} requires symbol")
    size_usd = _f(data.get("size_usd"), "size_usd")
    size = _i(data.get("size"), "size")
    size_pct = _f(data.get("size_pct"), "size_pct")
    margin_pct = _f(data.get("margin_pct"), "margin_pct")
    for name, val in (("size_pct", size_pct), ("margin_pct", margin_pct)):
        if val is not None and not (0 < val <= 1):
            raise SchemaError(f"{name} must be in (0, 1], got {val}")
    if size_usd is None and size is None and size_pct is None and margin_pct is None:
        raise SchemaError(f"{action} requires size_usd, size_pct, margin_pct, or size")

    order_type = str(data.get("type") or "market").lower()
    if order_type not in ORDER_TYPES:
        raise SchemaError(f"unsupported type: {order_type!r}")
    price = _f(data.get("price"), "price")
    if order_type != "market" and price is None:
        raise SchemaError(f"type={order_type} requires price")
    if order_type == "market" and price is not None and data.get("price") != 0:
        # allow explicit null; reject non-zero market price to avoid silent limit
        pass

    tp = _f(data.get("tp"), "tp")
    sl = _f(data.get("sl"), "sl")
    tp_type = str(data.get("tp_type") or "limit").lower()
    sl_type = str(data.get("sl_type") or "limit").lower()
    if tp is not None and tp_type not in ORDER_TYPES:
        raise SchemaError(f"tp_type invalid: {tp_type!r}")
    if sl is not None and sl_type not in ORDER_TYPES:
        raise SchemaError(f"sl_type invalid: {sl_type!r}")
    # real close-trigger must be limit (mirror quick_order)
    if tp is not None and tp_type == "market":
        raise SchemaError("tp_type=market forbidden for close trigger; use limit")
    if sl is not None and sl_type == "market":
        raise SchemaError("sl_type=market forbidden for close trigger; use limit")

    trigger_price_type = str(data.get("trigger_price_type") or "latest").lower()
    if trigger_price_type not in PRICE_TYPES:
        raise SchemaError(f"trigger_price_type invalid: {trigger_price_type!r}")

    rule_tp = _i(data.get("trigger_rule_tp"), "trigger_rule_tp")
    rule_sl = _i(data.get("trigger_rule_sl"), "trigger_rule_sl")
    if rule_tp is None:
        rule_tp = infer_trigger_rules(action, is_tp=True)
    if rule_sl is None:
        rule_sl = infer_trigger_rules(action, is_tp=False)
    for rule in (rule_tp, rule_sl):
        if rule not in (1, 2):
            raise SchemaError("trigger rule must be 1 or 2")

    leverage = _i(data.get("leverage"), "leverage")
    if leverage is not None and leverage <= 0:
        raise SchemaError("leverage must be positive")
    if size is not None and size <= 0:
        raise SchemaError("size must be positive")
    if size_usd is not None and size_usd <= 0:
        raise SchemaError("size_usd must be positive")

    margin_mode = data.get("margin_mode")
    if margin_mode is not None:
        margin_mode = str(margin_mode).lower()
        if margin_mode not in ("cross", "isolated"):
            raise SchemaError("margin_mode must be cross|isolated")

    return Intent(
        action=action,
        symbol=resolve_symbol(symbol),
        size_usd=size_usd,
        size_pct=size_pct,
        margin_pct=margin_pct,
        size=size,
        order_type=order_type,
        price=price,
        leverage=leverage,
        tp=tp,
        sl=sl,
        tp_type=tp_type,
        sl_type=sl_type,
        tp_limit_price=_f(data.get("tp_limit_price"), "tp_limit_price"),
        sl_limit_price=_f(data.get("sl_limit_price"), "sl_limit_price"),
        trigger_price_type=trigger_price_type,
        trigger_rule_tp=rule_tp,
        trigger_rule_sl=rule_sl,
        trigger_expiration=_i(data.get("trigger_expiration"), "trigger_expiration"),
        margin_mode=margin_mode,
        side="long" if action == "open_long" else "short",
        label=str(data.get("label") or default_label),
        meta=data.get("meta") or {},
    )


def _parse_grid(data: dict, default_label: str) -> Intent:
    """Expand grid into a container Intent; executor treats action=grid as multi open."""
    symbol = data.get("symbol")
    if not symbol:
        raise SchemaError("grid requires symbol")
    side = str(data.get("side") or "").lower()
    if side not in ("long", "short"):
        raise SchemaError("grid.side must be long|short")
    levels = data.get("levels")
    if not isinstance(levels, list) or not levels:
        raise SchemaError("grid.levels must be a non-empty array")
    if len(levels) > 50:
        raise SchemaError("grid.levels too large (max 50)")

    order_type = str(data.get("type") or "limit").lower()
    if order_type == "market":
        raise SchemaError("grid requires limit-style levels with price")
    if order_type not in ORDER_TYPES:
        raise SchemaError(f"unsupported type: {order_type!r}")

    label = str(data.get("label") or default_label)
    tp = _f(data.get("tp"), "tp")
    sl = _f(data.get("sl"), "sl")
    meta = data.get("meta") or {}

    # Represent grid as Intent with action=grid; expanded in expand_signal().
    # Store normalized levels in meta for expansion.
    norm_levels = []
    for idx, lv in enumerate(levels):
        if not isinstance(lv, dict):
            raise SchemaError(f"levels[{idx}] must be object")
        price = _f(lv.get("price"), f"levels[{idx}].price")
        if price is None:
            raise SchemaError(f"levels[{idx}].price required")
        size_usd = _f(lv.get("size_usd"), f"levels[{idx}].size_usd")
        size = _i(lv.get("size"), f"levels[{idx}].size")
        if size_usd is None and size is None:
            raise SchemaError(f"levels[{idx}] requires size_usd or size")
        norm_levels.append({"price": price, "size_usd": size_usd, "size": size})

    open_action = "open_long" if side == "long" else "open_short"
    return Intent(
        action="grid",
        symbol=resolve_symbol(symbol),
        side=side,
        order_type=order_type,
        tp=tp,
        sl=sl,
        tp_type=str(data.get("tp_type") or "limit").lower(),
        sl_type=str(data.get("sl_type") or "limit").lower(),
        tp_limit_price=_f(data.get("tp_limit_price"), "tp_limit_price"),
        sl_limit_price=_f(data.get("sl_limit_price"), "sl_limit_price"),
        trigger_price_type=str(data.get("trigger_price_type") or "latest").lower(),
        trigger_rule_tp=_i(data.get("trigger_rule_tp"), "trigger_rule_tp")
        or infer_trigger_rules(open_action, True),
        trigger_rule_sl=_i(data.get("trigger_rule_sl"), "trigger_rule_sl")
        or infer_trigger_rules(open_action, False),
        trigger_expiration=_i(data.get("trigger_expiration"), "trigger_expiration"),
        leverage=_i(data.get("leverage"), "leverage"),
        margin_mode=str(data["margin_mode"]).lower() if data.get("margin_mode") else None,
        label=label,
        meta={**(meta or {}), "levels": norm_levels},
    )


def parse_signal(data: Any, default_label: str = "signal") -> SignalFile:
    if not isinstance(data, dict):
        raise SchemaError("signal root must be a JSON object")
    if "orders" in data and "action" in data:
        raise SchemaError("use either top-level action or orders[], not both")
    if "orders" in data:
        orders = data.get("orders")
        if not isinstance(orders, list):
            raise SchemaError("orders must be an array")
        # empty orders[] is a legal no-op (hold → done)
        if not orders:
            return SignalFile(intents=[Intent(action="hold", meta=data.get("meta") or {})], meta=data.get("meta") or {}, raw=data)
        if len(orders) > 50:
            raise SchemaError("orders too large (max 50)")
        intents = [parse_intent(item, default_label=default_label) for item in orders]
        return SignalFile(intents=intents, meta=data.get("meta") or {}, raw=data)
    if "action" in data:
        intent = parse_intent(data, default_label=default_label)
        return SignalFile(intents=[intent], meta=data.get("meta") or {}, raw=data)
    # bare object without action/orders is a legal hold no-op
    return SignalFile(
        intents=[Intent(action="hold", meta=data.get("meta") or {})],
        meta=data.get("meta") or {},
        raw=data,
    )


def expand_signal(signal: SignalFile) -> list[Intent]:
    """Expand grid intents into open_long/open_short intents."""
    out: list[Intent] = []
    for intent in signal.intents:
        if intent.action != "grid":
            out.append(intent)
            continue
        levels = intent.meta.get("levels") or []
        open_action = "open_long" if intent.side == "long" else "open_short"
        # only attach tp/sl to the last level to avoid duplicate close-triggers
        for i, lv in enumerate(levels):
            last = i == len(levels) - 1
            out.append(
                Intent(
                    action=open_action,
                    symbol=intent.symbol,
                    size_usd=lv.get("size_usd"),
                    size=lv.get("size"),
                    order_type=intent.order_type,
                    price=lv.get("price"),
                    leverage=intent.leverage,
                    tp=intent.tp if last else None,
                    sl=intent.sl if last else None,
                    tp_type=intent.tp_type,
                    sl_type=intent.sl_type,
                    tp_limit_price=intent.tp_limit_price if last else None,
                    sl_limit_price=intent.sl_limit_price if last else None,
                    trigger_price_type=intent.trigger_price_type,
                    trigger_rule_tp=intent.trigger_rule_tp,
                    trigger_rule_sl=intent.trigger_rule_sl,
                    trigger_expiration=intent.trigger_expiration,
                    margin_mode=intent.margin_mode,
                    side=intent.side,
                    label=f"{intent.label}-g{i+1}",
                    meta={**intent.meta, "grid_index": i + 1, "grid_total": len(levels)},
                )
            )
    return out
