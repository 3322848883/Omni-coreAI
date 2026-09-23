"""Execute parsed intents against Gate.io."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .gate_client import GateApiError, GateClient, resolve_symbol
from .schema import Intent, SignalFile, expand_signal
from .sizing import default_trigger_limit_price, pct_to_size_usd, round_price, usd_to_contracts

log = logging.getLogger("gate_bot.executor")

PRICE_TYPE_MAP = {"latest": 0, "mark": 1, "index": 2}


@dataclass
class StepResult:
    action: str
    symbol: str
    ok: bool
    detail: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class ExecReport:
    results: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "steps": [
                {
                    "action": r.action,
                    "symbol": r.symbol,
                    "ok": r.ok,
                    "detail": r.detail,
                    "error": r.error,
                }
                for r in self.results
            ],
        }


class Executor:
    def __init__(
        self,
        client: GateClient,
        symbols_whitelist: Optional[list[str]] = None,
        max_notional_usd: Optional[float] = None,
        position_policy: str = "free",
        default_replace: str = "none",
    ):
        self.client = client
        self.symbols_whitelist = (
            {resolve_symbol(s) for s in symbols_whitelist} if symbols_whitelist else None
        )
        self.max_notional_usd = max_notional_usd
        self.position_policy = (position_policy or "free").lower()
        self.default_replace = (default_replace or "none").lower()

    def execute_signal(self, signal: SignalFile) -> ExecReport:
        report = ExecReport()
        intents = expand_signal(signal)
        if not intents:
            report.results.append(
                StepResult("hold", "", True, detail={"skipped": True})
            )
            return report
        # anti-pile-up: cancel old resting orders before new plan
        self._apply_replace(signal, intents, report)
        for intent in intents:
            gate = self._entry_gate(intent)
            if gate:
                report.results.append(
                    StepResult(intent.action, intent.symbol, False, error=gate)
                )
                break
            try:
                step = self._execute_intent(intent)
            except GateApiError as e:
                step = StepResult(intent.action, intent.symbol, False, error=str(e))
            except Exception as e:  # noqa: BLE001 — boundary
                step = StepResult(intent.action, intent.symbol, False, error=repr(e))
            requested = (intent.meta or {}).get("requested_action")
            if requested and requested != step.action:
                step = StepResult(
                    action=requested,
                    symbol=step.symbol,
                    ok=step.ok,
                    detail={**step.detail, "executed_as": step.action},
                    error=step.error,
                )
            report.results.append(step)
            if not step.ok:
                break
        return report

    def _entry_gate(self, intent: Intent) -> str:
        """Return error string to reject intent, or '' to allow.

        position_policy:
          free        — always allow
          manage_only — with position on symbol: allow manage (add/reduce/close/cancel/hold/trail),
                        reject new entry (open_*/stop_entry_*)
          strict      — like manage_only; opposite-side add_* also rejected
        """
        policy = self.position_policy
        if policy not in ("strict", "manage_only"):
            return ""
        action = intent.action
        manage = {
            "hold", "close", "close_all", "flatten",
            "cancel_all", "cancel_price_all", "cancel_trail_all",
            "reduce", "reduce_long", "reduce_short", "trail",
        }
        # normalized after alias map: open_*, stop_entry_*, add_* via requested
        requested = (intent.meta or {}).get("requested_action") or action
        if action in manage or requested in manage:
            return ""
        if action == "hold":
            return ""
        # entry-like: open_long/open_short/stop_entry_*
        pos = self._symbol_positions(intent.symbol)
        if not pos:
            return ""
        # allow same-side add_* only
        if requested in ("add_long", "open_long") and all(p["side"] == "long" for p in pos):
            if requested == "add_long":
                return ""
            # open_long with existing long = new plan pile-up → reject under both
            return f"POSITION_EXISTS: {intent.symbol} already has position; use add_long/reduce or replace plan first"
        if requested in ("add_short", "open_short") and all(p["side"] == "short" for p in pos):
            if requested == "add_short":
                return ""
            return f"POSITION_EXISTS: {intent.symbol} already has position; use add_short/reduce or replace plan first"
        if policy == "strict" and requested in ("add_long", "add_short"):
            return f"POSITION_POLICY_STRICT: opposite add not allowed while position open on {intent.symbol}"
        return (
            f"POSITION_EXISTS: {intent.symbol} has open position "
            f"({'/'.join(sorted({p['side'] for p in pos}))}); "
            f"policy={policy} only allows add/reduce/close/tp-sl"
        )

    def _symbol_positions(self, symbol: str) -> list[dict]:
        try:
            positions = self.client.get_positions() or []
        except Exception:  # noqa: BLE001
            return []
        out = []
        for p in positions:
            if p.get("contract") != symbol:
                continue
            size = int(p.get("size") or 0)
            if size == 0:
                continue
            mode = str(p.get("mode") or "")
            if size > 0 or mode.endswith("long"):
                side = "long"
            else:
                side = "short"
            out.append({"side": side, "size": size, "mode": mode})
        return out

    def _apply_replace(self, signal: SignalFile, intents: list, report: ExecReport) -> None:
        """Cancel old open/price orders before executing a new plan.

        replace=all    → every whitelist symbol (or all open contracts)
        replace=symbol → each symbol in this payload (once)
        """
        modes = {getattr(i, "replace", "none") for i in intents}
        if signal.replace and signal.replace != "none":
            modes.add(signal.replace)
        mode = "all" if "all" in modes else ("symbol" if "symbol" in modes else "none")
        if mode == "none":
            return
        if mode == "all":
            symbols = sorted(self.symbols_whitelist) if self.symbols_whitelist else self._open_symbols()
        else:
            symbols = sorted({i.symbol for i in intents if i.symbol})
        for sym in symbols:
            try:
                self.client.cancel_all_orders(sym)
                self.client.cancel_all_price_orders(sym)
                report.results.append(
                    StepResult(
                        "replace_cancel",
                        sym,
                        True,
                        detail={"replace": mode, "cancelled": ["orders", "price_orders"]},
                    )
                )
            except GateApiError as e:
                report.results.append(
                    StepResult("replace_cancel", sym, False, error=str(e))
                )

    def _execute_intent(self, intent: Intent) -> StepResult:
        print(self.client.banner())
        action = intent.action
        if action == "hold":
            return StepResult("hold", "", True, detail={"skipped": True})
        if action == "close_all":
            return self._close_all(intent.symbol)
        if action == "cancel_all":
            return self._cancel_all(intent.symbol)
        if action == "cancel_price_all":
            return self._cancel_price_all(intent.symbol)
        if action in ("stop_entry_long", "stop_entry_short"):
            return self._stop_entry(intent)
        if action == "close":
            return self._close(intent)
        if action == "trail":
            return self._trail(intent)
        if action == "cancel_trail_all":
            return self._cancel_trail_all(intent.symbol)
        if action in ("open_long", "open_short"):
            return self._open(intent)
        return StepResult(action, intent.symbol, False, error=f"unhandled action {action}")

    # ── guards ───────────────────────────────────────────
    def _check_symbol(self, symbol: str) -> None:
        if not symbol:
            raise GateApiError("symbol required")
        if self.symbols_whitelist is not None and symbol not in self.symbols_whitelist:
            raise GateApiError(f"symbol {symbol} not in whitelist {sorted(self.symbols_whitelist)}")

    def _check_notional(self, size_usd: Optional[float]) -> None:
        if self.max_notional_usd is not None and size_usd is not None:
            if size_usd > self.max_notional_usd:
                raise GateApiError(
                    f"size_usd={size_usd} exceeds max_notional_usd={self.max_notional_usd}"
                )

    # ── actions ──────────────────────────────────────────
    def _open(self, intent: Intent) -> StepResult:
        self._check_symbol(intent.symbol)
        meta = self.client.get_contract(intent.symbol)
        self._check_notional(intent.size_usd)

        if intent.leverage:
            self.client.set_leverage(intent.symbol, int(intent.leverage))
        if intent.margin_mode:
            self.client.set_margin_mode(intent.symbol, intent.margin_mode)

        if intent.size is not None:
            contracts = int(intent.size)
        else:
            size_usd = intent.size_usd
            if size_usd is None and intent.size_pct is not None:
                size_usd = pct_to_size_usd(intent.size_pct, self.client.get_available_usdt())
            elif size_usd is None and intent.margin_pct is not None:
                size_usd = pct_to_size_usd(intent.margin_pct, self.client.get_available_usdt()) * int(intent.leverage or 1)
            entry = intent.price if intent.price is not None else self.client.get_last_price(intent.symbol)
            contracts = usd_to_contracts(float(size_usd), float(entry), meta)

        # Gate: positive size = buy/long, negative = sell/short
        order_size = contracts if intent.action == "open_long" else -contracts
        body: dict[str, Any] = {"contract": intent.symbol, "size": order_size}
        self._apply_order_type(body, intent.order_type, intent.price, meta)
        if intent.label:
            body["text"] = f"t-{intent.label}"

        order = self.client.place_order(body)
        detail: dict[str, Any] = {
            "order": order,
            "contracts": contracts,
            "size_usd": intent.size_usd,
            "order_type": intent.order_type,
            "price": intent.price,
            "quanto_multiplier": meta.quanto_multiplier,
        }

        # auto TP/SL close-trigger
        pos_side = "long" if intent.action == "open_long" else "short"
        trigger_side = "short" if pos_side == "long" else "long"
        tp_orders = []
        sl_orders = []
        if intent.tp is not None:
            tp_orders.append(self._place_trigger(intent, trigger_side, intent.tp, is_tp=True, meta=meta, size=contracts))
        if intent.sl is not None:
            sl_orders.append(self._place_trigger(intent, trigger_side, intent.sl, is_tp=False, meta=meta, size=contracts))
        detail["tp_orders"] = tp_orders
        detail["sl_orders"] = sl_orders
        return StepResult(intent.action, intent.symbol, True, detail=detail)

    def _trail(self, intent: Intent) -> StepResult:
        self._check_symbol(intent.symbol)
        amount = abs(int(intent.size or 0))
        if intent.side != "long":
            amount = -amount
        body = {
            "contract": intent.symbol,
            "amount": str(amount),
            "activation_price": str(intent.activation_price or "0"),
            "price_offset": str(intent.price_offset),
        }
        order = self.client.place_trailing_order(body)
        return StepResult("trail", intent.symbol, True, detail={"order": order, "body": body})

    def _cancel_trail_all(self, symbol: str) -> StepResult:
        if symbol:
            self._check_symbol(symbol)
        result = self.client.stop_trailing_orders(symbol or None)
        return StepResult("cancel_trail_all", symbol, True, detail={"result": result})

    def _stop_entry(self, intent: Intent) -> StepResult:
        """Breakout ENTRY: trigger then OPEN. Not stop-loss."""
        self._check_symbol(intent.symbol)
        meta = self.client.get_contract(intent.symbol)
        self._check_notional(intent.size_usd)
        if intent.size is not None:
            contracts = int(intent.size)
        else:
            size_usd = intent.size_usd
            if size_usd is None and intent.size_pct is not None:
                size_usd = pct_to_size_usd(intent.size_pct, self.client.get_available_usdt())
            elif size_usd is None and intent.margin_pct is not None:
                size_usd = pct_to_size_usd(intent.margin_pct, self.client.get_available_usdt()) * int(intent.leverage or 1)
            entry = intent.price if intent.price is not None else self.client.get_last_price(intent.symbol)
            contracts = usd_to_contracts(float(size_usd), float(entry), meta)
        signed = contracts if intent.action == "stop_entry_long" else -contracts
        if intent.order_type == "market":
            initial = {"contract": intent.symbol, "size": signed, "price": "0", "tif": "ioc"}
        else:
            initial = {
                "contract": intent.symbol,
                "size": signed,
                "price": str(round_price(float(intent.price), meta)),
                "tif": {"limit": "gtc", "post_only": "poc", "ioc": "ioc", "fok": "fok"}[intent.order_type],
            }
        if intent.label:
            initial["text"] = f"t-{intent.label}"
        body = {
            "initial": initial,
            "trigger": {
                "strategy_type": 0,
                "price_type": PRICE_TYPE_MAP.get(intent.trigger_price_type, 0),
                "price": str(intent.trigger_price_tp),
                "rule": int(intent.trigger_rule_tp or (1 if intent.action == "stop_entry_long" else 2)),
            },
        }
        if intent.trigger_expiration and self.client.env == "live":
            body["trigger"]["expiration"] = int(intent.trigger_expiration)
        order = self.client.place_price_order(body)
        return StepResult(intent.action, intent.symbol, True, detail={"order": order, "body": body})

    def _close(self, intent: Intent) -> StepResult:
        self._check_symbol(intent.symbol)
        dual = self.client.is_dual_position_mode()
        requested = (intent.meta or {}).get("requested_action") or intent.action
        side = intent.side
        if requested == "reduce_long":
            side = "long"
        elif requested == "reduce_short":
            side = "short"
        if dual and side not in ("long", "short"):
            raise GateApiError(
                "dual position mode requires side (use reduce_long / reduce_short or side=long|short)"
            )
        size = intent.close_size
        if size is not None and size <= 0:
            raise GateApiError("reduce size must be positive")
        order = self.client.close_position(
            intent.symbol,
            side=side if dual else side,
            size=size or 0,
        )
        return StepResult(
            requested,
            intent.symbol,
            True,
            detail={
                "order": order,
                "side": side,
                "position_mode": self.client.get_position_mode(),
                "executed_as": "close",
                "mode_note": (
                    "dual: close this side only" if dual else "single: one book, side is advisory"
                ),
            },
        )

    def _close_all(self, symbol: str) -> StepResult:
        symbols = [symbol] if symbol else self._open_symbols()
        if symbol:
            self._check_symbol(symbol)
        orders = []
        dual = self.client.is_dual_position_mode()
        for sym in symbols:
            if self.symbols_whitelist is not None and sym not in self.symbols_whitelist:
                continue
            if dual:
                for side in ("long", "short"):
                    try:
                        orders.append(self.client.close_position(sym, side=side).get("id"))
                    except GateApiError as e:
                        if "no position" in str(e).lower():
                            continue
                        raise
            else:
                try:
                    orders.append(self.client.close_position(sym, side=None).get("id"))
                except GateApiError as e:
                    if "no position" in str(e).lower():
                        continue
                    raise
        return StepResult("close_all", symbol, True, detail={"closed_order_ids": orders})

    def _cancel_all(self, symbol: str) -> StepResult:
        if symbol:
            self._check_symbol(symbol)
            result = self.client.cancel_all_orders(symbol)
        else:
            # cancel open orders on every contract with open orders (not only open positions)
            orders = self.client.list_orders() or []
            symbols = []
            for o in orders:
                sym = o.get("contract")
                if sym and sym not in symbols:
                    symbols.append(sym)
            result = {sym: self.client.cancel_all_orders(sym) for sym in symbols}
        return StepResult("cancel_all", symbol, True, detail={"result": result})

    def _cancel_price_all(self, symbol: str) -> StepResult:
        if symbol:
            self._check_symbol(symbol)
        result = self.client.cancel_all_price_orders(symbol or None)
        return StepResult("cancel_price_all", symbol, True, detail={"result": result})

    def _open_symbols(self) -> list[str]:
        positions = self.client.get_positions() or []
        symbols = []
        for p in positions:
            sym = p.get("contract")
            if sym and int(p.get("size") or 0) != 0 and sym not in symbols:
                symbols.append(sym)
        return symbols

    def _apply_order_type(self, body: dict, order_type: str, price: Optional[float], meta) -> None:
        if order_type == "market":
            body["price"] = "0"
            body["tif"] = "ioc"
            return
        if price is None:
            raise GateApiError(f"type={order_type} requires price")
        body["price"] = str(round_price(float(price), meta))
        body["tif"] = {
            "limit": "gtc",
            "post_only": "poc",
            "ioc": "ioc",
            "fok": "fok",
        }[order_type]

    def _place_trigger(
        self,
        intent: Intent,
        trigger_side: str,
        trigger_price: float,
        is_tp: bool,
        meta,
        size: int,
    ) -> dict:
        """Place a close price-trigger (TP/SL). trigger_side is the order side to close position."""
        if is_tp:
            rule = intent.trigger_rule_tp
            order_type = intent.tp_type
            limit = intent.tp_limit_price
        else:
            rule = intent.trigger_rule_sl
            order_type = intent.sl_type
            limit = intent.sl_limit_price

        if order_type == "market":
            raise GateApiError("close trigger market forbidden; use limit")
        if limit is None:
            limit = default_trigger_limit_price(float(trigger_price), intent.side or "long", is_tp)
        limit = round_price(float(limit), meta)

        # size for close-trigger: opposite side contracts; 0 would mean full close —
        # use explicit integer size of the opened contracts.
        close_size = int(size)
        # API: buy to close short (positive), sell to close long (negative)
        api_size = -close_size if (intent.side or "long") == "long" else close_size

        body: dict[str, Any] = {
            "initial": {
                "contract": intent.symbol,
                "size": api_size,
                "price": str(limit),
                "tif": "gtc",
                "reduce_only": True,
                "text": f"t-{intent.label}-{'tp' if is_tp else 'sl'}",
            },
            "trigger": {
                "strategy_type": 0,
                "price_type": PRICE_TYPE_MAP.get(intent.trigger_price_type, 0),
                "price": str(trigger_price),
                "rule": int(rule),
            },
        }
        if intent.trigger_expiration and self.client.env == "live":
            body["trigger"]["expiration"] = int(intent.trigger_expiration)
        if intent.margin_mode:
            body["pos_margin_mode"] = intent.margin_mode

        # Gate price_orders body is nested {initial, trigger} (same as quick_order.cmd_trigger_order)
        return self.client.place_price_order(body)
