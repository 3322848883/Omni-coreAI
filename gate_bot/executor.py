"""Execute parsed intents against Gate.io."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .gate_client import GateApiError, GateClient, resolve_symbol
from .schema import Intent, SignalFile, expand_signal
from .sizing import default_trigger_limit_price, pct_to_size_usd, round_price, usd_to_contracts

log = logging.getLogger("gate_bot.executor")


def _is_flat_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "no position" in msg or "position_empty" in msg

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
        order_scope: str = "own",
        require_sl: bool = True,
        account_risk: Optional[dict] = None,
        label_prefix: str = "",
    ):
        self.client = client
        self.symbols_whitelist = (
            {resolve_symbol(s) for s in symbols_whitelist} if symbols_whitelist else None
        )
        self.max_notional_usd = max_notional_usd
        self.position_policy = (position_policy or "free").lower()
        self.default_replace = (default_replace or "none").lower()
        # own = only cancel this bot's orders (text prefix t-{label}); all = legacy wipe
        self.order_scope = (order_scope or "own").lower()
        # pre-trade: open_* must carry sl (unless explicitly disabled)
        self.require_sl = bool(require_sl)
        # account-level risk: halt / max_total_notional_usd / daily_loss_limit_usd / max_leverage
        self.account_risk = dict(account_risk or {})
        # bot namespace: order texts always under t-{label_prefix}*; signal labels cannot escape
        self.label_prefix = str(label_prefix or "").strip()

    def execute_signal(self, signal: SignalFile) -> ExecReport:
        report = ExecReport()
        intents = expand_signal(signal)
        if not intents:
            report.results.append(
                StepResult("hold", "", True, detail={"skipped": True})
            )
            return report
        # Snapshot owned resting orders before new placements (place-before-cancel).
        # Do NOT cancel here — new protection first, then withdraw old owned ones.
        replace_mode = self._replace_mode(signal, intents)
        pre_owned = self._snapshot_owned(intents) if replace_mode != "none" else {}

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

        # After successful steps: withdraw OLD owned orders (keep newly placed ids)
        if replace_mode != "none" and report.ok:
            self._cancel_stale_owned(pre_owned, intents, report)
        return report

    def _replace_mode(self, signal: SignalFile, intents: list) -> str:
        modes = {getattr(i, "replace", "none") for i in intents}
        if signal.replace and signal.replace != "none":
            modes.add(signal.replace)
        if self.default_replace and self.default_replace != "none":
            modes.add(self.default_replace)
        return "all" if "all" in modes else ("symbol" if "symbol" in modes else "none")

    def _text_prefix(self, label: str) -> str:
        return f"t-{label}" if label else ""

    def _bot_tag(self, label: str) -> str:
        """Ownership tag. When label_prefix is set, signal labels cannot escape the bot namespace."""
        label = (label or "signal").strip()
        if self.label_prefix:
            if label == self.label_prefix or label.startswith(self.label_prefix + "-"):
                return label
            return f"{self.label_prefix}-{label}"
        return label

    def _own_scope_tag(self, label: str) -> str:
        """Tag used to filter cancel/replace under order_scope=own."""
        if self.label_prefix:
            return self.label_prefix
        return (label or "").strip()

    def _own_prefix(self, label: str = "") -> str:
        """text prefix used for own-scope ownership filtering."""
        tag = self._own_scope_tag(label)
        return self._text_prefix(tag)

    def _text_owned(self, text: str, label: str) -> bool:
        """Segment-safe ownership: t-{label} or t-{label}-*, never t-{label}XX."""
        if not label:
            return False
        text = str(text or "")
        tag = f"t-{label}"
        return text == tag or text.startswith(tag + "-")

    def _owned_price_orders(self, symbol: str, prefix: str) -> list[dict]:
        if not prefix:
            return []
        label = prefix[2:] if prefix.startswith("t-") else prefix
        try:
            rows = self.client.list_price_orders(symbol) or []
        except GateApiError:
            return []
        out = []
        for p in rows:
            text = str((p.get("initial") or {}).get("text") or p.get("text") or "")
            if self._text_owned(text, label):
                out.append(p)
        return out

    def _owned_open_orders(self, symbol: str, prefix: str) -> list[dict]:
        if not prefix:
            return []
        label = prefix[2:] if prefix.startswith("t-") else prefix
        try:
            rows = self.client.list_orders(symbol) or []
        except GateApiError:
            return []
        return [o for o in rows if self._text_owned(str(o.get("text") or ""), label)]

    def _snapshot_owned(self, intents: list) -> dict:
        """{symbol: {"prefix": set(price_ids), "orders": set(order_ids)}}"""
        snap: dict[str, dict] = {}
        for intent in intents:
            if not intent.symbol:
                continue
            prefix = self._own_prefix(intent.label)
            if not prefix:
                continue
            slot = snap.setdefault(intent.symbol, {"prefix": prefix, "price_ids": set(), "order_ids": set()})
            for p in self._owned_price_orders(intent.symbol, prefix):
                pid = str(p.get("id") or "")
                if pid:
                    slot["price_ids"].add(pid)
            for o in self._owned_open_orders(intent.symbol, prefix):
                oid = str(o.get("id") or "")
                if oid:
                    slot["order_ids"].add(oid)
        return snap

    def _cancel_stale_owned(self, pre_owned: dict, intents: list, report: ExecReport) -> None:
        """Cancel old owned orders that are not part of the new plan (place-before-cancel)."""
        if self.order_scope == "all":
            # legacy wipe after new plan is in place
            symbols = sorted({i.symbol for i in intents if i.symbol})
            if not symbols and self.symbols_whitelist:
                symbols = sorted(self.symbols_whitelist)
            elif not symbols:
                symbols = self._open_symbols()
            for sym in symbols:
                try:
                    self.client.cancel_all_orders(sym)
                    self.client.cancel_all_price_orders(sym)
                    report.results.append(StepResult("replace_cancel", sym, True,
                        detail={"replace": "all", "cancelled": ["orders", "price_orders"], "order_scope": "all"}))
                except GateApiError as e:
                    report.results.append(StepResult("replace_cancel", sym, False, error=str(e)))
            return

        # Collect new ids to keep
        keep_price: dict[str, set] = {}
        keep_orders: dict[str, set] = {}
        for intent in intents:
            if not intent.symbol:
                continue
            prefix = self._own_prefix(intent.label)
            keep_price.setdefault(intent.symbol, set())
            keep_orders.setdefault(intent.symbol, set())
            for p in self._owned_price_orders(intent.symbol, prefix):
                keep_price[intent.symbol].add(str(p.get("id") or ""))
            for o in self._owned_open_orders(intent.symbol, prefix):
                keep_orders[intent.symbol].add(str(o.get("id") or ""))

        for sym, slot in pre_owned.items():
            prefix = slot.get("prefix") or ""
            # pre_owned was snapshotted BEFORE this run — these are the old ones.
            # Newly placed orders are not in this set, so cancel the snapshot as-is.
            old_p = slot.get("price_ids") or set()
            old_o = slot.get("order_ids") or set()
            cancelled = []
            for pid in sorted(old_p):
                try:
                    self.client.cancel_price_order(pid)
                    cancelled.append(("price", pid))
                except GateApiError:
                    pass
            for oid in sorted(old_o):
                try:
                    self.client.cancel_order(oid)
                    cancelled.append(("order", oid))
                except GateApiError:
                    pass
            if cancelled:
                report.results.append(StepResult(
                    "replace_cancel", sym, True,
                    detail={"replace": "owned", "prefix": prefix, "cancelled": cancelled},
                ))

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
            return self._cancel_all(intent.symbol, intent.label or "")
        if action == "cancel_price_all":
            return self._cancel_price_all(intent.symbol, intent.label or "")
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

    def _check_open_sl(self, intent: Intent) -> None:
        """Reject naked entries: open_* / stop_entry_* must set sl."""
        if not self.require_sl:
            return
        action = (intent.meta or {}).get("requested_action") or intent.action
        if action not in ("open_long", "open_short", "stop_entry_long", "stop_entry_short"):
            return
        if intent.sl is None:
            raise GateApiError(
                "SL_REQUIRED: open/stop_entry requires sl (or set require_sl: false)"
            )

    def _check_account_risk(self, intent: Intent) -> None:
        """Account-level halt / exposure / daily-loss limits (pre-trade, all configurable)."""
        ar = self.account_risk or {}
        action = (intent.meta or {}).get("requested_action") or intent.action
        manage = {"hold", "close", "close_all", "flatten", "cancel_all", "cancel_price_all",
                  "cancel_trail_all", "reduce", "reduce_long", "reduce_short"}
        if ar.get("halt") and action not in manage:
            raise GateApiError("HALTED: account_risk.halt=true; only close/cancel allowed")
        if action not in manage and action.startswith(("open", "add", "stop_entry")):
            # daily loss vs day-start equity (strategy-adjustable)
            dlimit = ar.get("daily_loss_limit_usd")
            if dlimit is not None:
                try:
                    total = float((self.client.get_account() or {}).get("total") or 0)
                    start = self._day_start_equity(total)
                    if start > 0 and (start - total) > float(dlimit):
                        raise GateApiError(
                            f"DAILY_LOSS_LIMIT: loss={start - total:.2f} > {dlimit} "
                            f"(day_start={start:.2f} now={total:.2f})"
                        )
                except GateApiError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
            max_lev = ar.get("max_leverage")
            if max_lev is not None and intent.leverage and int(intent.leverage) > int(max_lev):
                raise GateApiError(
                    f"MAX_LEVERAGE: {intent.leverage} > account max_leverage={max_lev}"
                )
            # total notional: sum open positions + this order
            max_total = ar.get("max_total_notional_usd")
            if max_total is not None:
                try:
                    pos_notional = 0.0
                    for p in self.client.get_positions() or []:
                        if int(p.get("size") or 0) == 0:
                            continue
                        mark = float(p.get("mark_price") or p.get("entry_price") or 0)
                        qty = abs(float(p.get("size") or 0))
                        mult = 1.0
                        try:
                            mult = float(self.client.get_contract(p.get("contract")).quanto_multiplier or 1)
                        except Exception:  # noqa: BLE001
                            mult = 1.0
                        pos_notional += mark * qty * mult
                    this_usd = intent.size_usd
                    if this_usd is None and intent.size is not None:
                        px = intent.price or self.client.get_last_price(intent.symbol)
                        try:
                            mult = float(self.client.get_contract(intent.symbol).quanto_multiplier or 1)
                        except Exception:  # noqa: BLE001
                            mult = 1.0
                        this_usd = abs(intent.size) * float(px) * mult
                    if this_usd is not None and pos_notional + float(this_usd) > float(max_total):
                        raise GateApiError(
                            f"MAX_TOTAL_NOTIONAL: open={pos_notional:.2f} + this={this_usd:.2f} "
                            f"> {max_total}"
                        )
                except GateApiError:
                    raise
                except Exception:  # noqa: BLE001 — if cannot measure, do not silently allow huge size
                    raise GateApiError("MAX_TOTAL_NOTIONAL: cannot measure account exposure")

    def _day_start_equity(self, current_total: float) -> float:
        """Persist UTC-day start equity for daily_loss_limit (per bot process root)."""
        from datetime import datetime, timezone
        import json as _json

        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        root = Path(getattr(self, "state_dir", None) or Path.cwd() / "history" / "_account_risk")
        path = root / f"equity_{day}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                data = _json.loads(path.read_text(encoding="utf-8"))
                return float(data.get("start_equity") or 0)
            path.write_text(_json.dumps({"start_equity": current_total}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            return current_total
        return current_total

    def _check_notional(self, size_usd: Optional[float]) -> None:
        if self.max_notional_usd is not None and size_usd is not None:
            if size_usd > self.max_notional_usd:
                raise GateApiError(
                    f"size_usd={size_usd} exceeds max_notional_usd={self.max_notional_usd}"
                )

    # ── actions ──────────────────────────────────────────
    def _open(self, intent: Intent) -> StepResult:
        self._check_symbol(intent.symbol)
        self._check_open_sl(intent)
        self._check_account_risk(intent)
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
        if intent.label or self.label_prefix:
            body["text"] = f"t-{self._bot_tag(intent.label)}"

        entry_rec, entry_err = self._place_entry_leg(lambda: self.client.place_order(body), body)
        detail: dict[str, Any] = {
            "order": (entry_rec or {}).get("order") or {},
            "order_check": (entry_rec or {}).get("check") or {},
            "contracts": contracts,
            "size_usd": intent.size_usd,
            "order_type": intent.order_type,
            "price": intent.price,
            "quanto_multiplier": meta.quanto_multiplier,
            "leg_entry_ok": entry_rec is not None,
        }
        if entry_rec is None:
            return StepResult(
                intent.action, intent.symbol, False, detail=detail,
                error="entry_not_confirmed: " + str(entry_err),
            )

        # auto TP/SL: hang TOGETHER with entry (same planned size) — 不等成交再挂
        pos_side = "long" if intent.action == "open_long" else "short"
        trigger_side = "short" if pos_side == "long" else "long"
        entry_order = (entry_rec or {}).get("order") or {}
        filled = abs(int(entry_order.get("size") or 0)) - abs(int(entry_order.get("left") or 0))
        if entry_order.get("finish_as") == "filled":
            filled = abs(int(entry_order.get("size") or 0))
        # simultaneous hang: exit legs use the SAME size as entry plan
        exit_size = int(contracts)
        # grid shared TP/SL: one exit leg sized to total planned contracts
        if intent.tp_size_override and intent.tp is not None:
            exit_size = int(intent.tp_size_override)
        detail["exit_size"] = exit_size
        detail["filled_size"] = filled
        detail["hang_mode"] = "simultaneous"
        tp_orders = []
        sl_orders = []
        exit_errors = []
        if intent.tp is not None:
            rec_, err_ = self._place_exit_leg(
                lambda: self._place_limit_exit(intent, pos_side, intent.tp, is_tp=True, meta=meta, size=exit_size)
                if intent.tp_mode == "limit_order"
                else self._place_trigger(intent, trigger_side, intent.tp, is_tp=True, meta=meta, size=exit_size),
                kind="tp", price_order=(intent.tp_mode != "limit_order"),
            )
            (tp_orders if rec_ else exit_errors).append(rec_ or err_)
        if intent.sl is not None:
            rec_, err_ = self._place_exit_leg(
                lambda: self._place_limit_exit(intent, pos_side, intent.sl, is_tp=False, meta=meta, size=exit_size)
                if intent.sl_mode == "limit_order"
                else self._place_trigger(intent, trigger_side, intent.sl, is_tp=False, meta=meta, size=exit_size),
                kind="sl", price_order=(intent.sl_mode != "limit_order"),
            )
            (sl_orders if rec_ else exit_errors).append(rec_ or err_)
        detail["tp_orders"] = tp_orders
        detail["sl_orders"] = sl_orders
        detail["exit_errors"] = exit_errors
        detail["leg_tp_ok"] = intent.tp is None or bool(tp_orders)
        detail["leg_sl_ok"] = intent.sl is None or bool(sl_orders)
        detail["exits_ok"] = detail["leg_tp_ok"] and detail["leg_sl_ok"] and not exit_errors
        detail["all_legs_ok"] = detail["leg_entry_ok"] and detail["exits_ok"]
        if exit_errors or not detail["exits_ok"]:
            # entry landed but exits missing — do NOT pretend full success
            return StepResult(
                intent.action,
                intent.symbol,
                False,
                detail=detail,
                error="exit_not_placed: " + "; ".join(str(x) for x in exit_errors or ["tp/sl missing"]),
            )
        return StepResult(intent.action, intent.symbol, True, detail=detail)

    def _find_open_order_by_text(self, symbol: str, text: str) -> Optional[dict]:
        if not text:
            return None
        try:
            rows = self.client.list_orders(symbol) or []
        except Exception:  # noqa: BLE001
            return None
        for o in rows:
            if str(o.get("text") or "") == text:
                return o
        return None

    def _place_entry_leg(self, placer, body: dict) -> tuple[Optional[dict], Optional[str]]:
        """Place entry with confirmation; retry only when order is NOT on exchange."""
        text = str(body.get("text") or "")
        last_err = None
        for attempt in (1, 2):
            if attempt == 2:
                # idempotent: if first place landed but confirm flaked, do NOT double-place
                existing = self._find_open_order_by_text(body.get("contract"), text)
                if existing and existing.get("id"):
                    check = self._confirm_order_landed(existing, kind="entry")
                    return {"order": existing, "check": check}, None
            try:
                order = placer()
            except GateApiError as e:
                last_err = f"entry#{attempt} {e}"
                continue
            check = self._confirm_order_landed(order, kind="entry")
            if check.get("ok") and check.get("confirmed"):
                return {"order": order, "check": check}, None
            last_err = f"entry#{attempt} not_confirmed id={(order or {}).get('id')}"
        return None, last_err

    def _confirm_order_landed(self, order: dict, kind: str = "entry") -> dict:
        """Re-read the order on exchange; reject empty/failed placements."""
        oid = (order or {}).get("id")
        status = str((order or {}).get("status") or "")
        finish_as = str((order or {}).get("finish_as") or "")
        check = {
            "kind": kind,
            "id": oid,
            "status": status,
            "finish_as": finish_as,
            "left": (order or {}).get("left"),
            "ok": bool(oid) and status not in ("cancelled", "failed", "reduced"),
        }
        if not check["ok"]:
            return check
        try:
            live = self.client.get_order(str(oid))
            check["live_status"] = live.get("status")
            check["live_left"] = live.get("left")
            # still visible as open or already finished/canceled-after-fill
            check["confirmed"] = live.get("id") is not None
        except Exception as e:  # noqa: BLE001
            check["confirm_error"] = str(e)
            check["confirmed"] = False
        return check

    def _confirm_price_order_landed(self, order: dict, kind: str = "trigger") -> dict:
        oid = (order or {}).get("id")
        check = {"kind": kind, "id": oid, "ok": bool(oid), "confirmed": False}
        if not oid:
            return check
        try:
            live = self.client.get_price_order(str(oid))
            check["live_status"] = live.get("status")
            check["confirmed"] = live.get("id") is not None and str(live.get("status") or "") != "cancelled"
        except Exception as e:  # noqa: BLE001
            # fall back to open list
            try:
                found = [
                    p for p in (self.client.list_price_orders(order.get("body", {}).get("initial", {}).get("contract"))
                                or [])
                    if str(p.get("id")) == str(oid)
                ]
                check["confirmed"] = bool(found)
            except Exception as e2:  # noqa: BLE001
                check["confirm_error"] = f"{e} / {e2}"
        return check

    def _place_exit_leg(self, placer, kind: str, price_order: bool) -> tuple[Optional[dict], Optional[str]]:
        """Place one exit leg with confirmation; retry only if not on exchange."""
        last_err = None
        for attempt in (1, 2):
            if attempt == 2 and last_err and "not_confirmed" in str(last_err):
                # do not blindly re-place when first attempt may have landed
                break
            try:
                rec = placer()
            except GateApiError as e:
                last_err = f"{kind}#{attempt} {e}"
                continue
            raw = dict(rec or {})
            inner = raw.get("order")
            if isinstance(inner, dict):
                order = dict(inner)
            else:
                # raw is the order payload itself (place_price_order / _place_trigger)
                order = {k: v for k, v in raw.items() if k not in ("order", "check")}
            check = (
                self._confirm_price_order_landed(order, kind=kind)
                if price_order
                else self._confirm_order_landed(order, kind=kind)
            )
            rec = dict(raw)
            rec["order"] = order
            rec["check"] = check
            if check.get("ok", True) and (check.get("confirmed") or order.get("finish_as") == "finished"):
                return rec, None
            last_err = f"{kind}#{attempt} not_confirmed id={order.get('id')}"
        return None, last_err or f"{kind} failed"

    def _place_limit_exit(self, intent: Intent, pos_side: str, price: float, is_tp: bool, meta, size: int) -> dict:
        """Resting reduce_only LIMIT exit (限价止盈/止损挂单), not a price trigger.

        long  → sell limit (negative size)
        short → buy limit (positive size)
        Note: a stop-loss limit that is immediately marketable will fill at once;
        use sl_mode=trigger for true stop semantics.
        """
        api_size = -abs(size) if pos_side == "long" else abs(size)
        px = float(price)

        def _body(p: float) -> dict:
            return {
                "contract": intent.symbol,
                "size": api_size,
                "price": str(round_price(p, meta)),
                "tif": "gtc",
                "reduce_only": True,
                "text": f"t-{self._bot_tag(intent.label)}-{'lp' if is_tp else 'ls'}",
            }

        body = _body(px)
        try:
            order = self.client.place_order(body)
        except GateApiError as e:
            if "PRICE_TOO_DEVIATED" not in str(e) and "MARKET_PRICE_TOO_DEVIATED" not in str(e):
                raise
            # one clamp toward book mid so resting exit still lands
            mid = self._book_mid(intent.symbol)
            if mid is None:
                raise
            side = "down" if pos_side == "long" else "up"
            clamped = self._clamp_toward(px, mid, side=side, max_bps=80)
            body = _body(clamped)
            order = self.client.place_order(body)
            body["price_clamped_from"] = round_price(px, meta)
        return {
            "mode": "limit_order",
            "kind": "tp" if is_tp else "sl",
            "order": order,
            "body": body,
        }

    def _book_mid(self, symbol: str) -> Optional[float]:
        try:
            ob = self.client.get_orderbook_top(symbol, limit=1)
            bids, asks = ob.get("bids") or [], ob.get("asks") or []
            b = float((bids[0] or {}).get("p")) if bids else None
            a = float((asks[0] or {}).get("p")) if asks else None
            if b is not None and a is not None:
                return (b + a) / 2.0
            return b or a
        except Exception:  # noqa: BLE001
            return None

    def _clamp_toward(self, px: float, mid: float, side: str = "down", max_bps: float = 80) -> float:
        limit = mid * (1 - max_bps / 10000.0) if side == "down" else mid * (1 + max_bps / 10000.0)
        return min(px, limit) if side == "down" else max(px, limit)

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
        if intent.label or self.label_prefix:
            initial["text"] = f"t-{self._bot_tag(intent.label)}"
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
        check = self._confirm_price_order_landed(order, kind="stop_entry")
        ok = bool(check.get("ok") and check.get("confirmed"))
        detail = {"order": order, "body": body, "order_check": check}
        if not ok:
            return StepResult(intent.action, intent.symbol, False, detail=detail,
                              error=f"stop_entry not confirmed id={order.get('id')}")
        return StepResult(intent.action, intent.symbol, True, detail=detail)

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
                        oid = self.client.close_position(sym, side=side).get("id")
                        if oid is not None:
                            orders.append(oid)
                    except GateApiError as e:
                        if _is_flat_error(e):
                            continue
                        raise
            else:
                try:
                    oid = self.client.close_position(sym, side=None).get("id")
                    if oid is not None:
                        orders.append(oid)
                except GateApiError as e:
                    if _is_flat_error(e):
                        continue
                    raise
        return StepResult("close_all", symbol, True, detail={"closed_order_ids": orders})

    def _cancel_all(self, symbol: str, label: str = "") -> StepResult:
        prefix = self._own_prefix(label)
        # own-scope isolation for bot namespace / explicit label; default "signal"
        # keeps legacy wipe so cleanup/manage paths still work.
        own_tag = self._own_scope_tag(label)
        use_own = self.order_scope == "own" and prefix and own_tag not in ("", "signal")
        if use_own:
            # only this bot's open orders (text prefix), never wipe the book
            if symbol:
                self._check_symbol(symbol)
                symbols = [symbol]
            else:
                rows = self.client.list_orders() or []
                symbols = sorted({o.get("contract") for o in rows if o.get("contract")})
            cancelled = []
            errors = []
            for sym in symbols:
                for o in self._owned_open_orders(sym, prefix):
                    oid = str(o.get("id") or "")
                    if not oid:
                        continue
                    try:
                        self.client.cancel_order(oid)
                        cancelled.append(oid)
                    except GateApiError as e:
                        errors.append(f"{oid}: {e}")
            ok = not errors
            return StepResult("cancel_all", symbol, ok, detail={
                "cancelled": cancelled, "errors": errors,
                "order_scope": "own", "prefix": prefix,
            }, error="; ".join(errors) if errors else None)
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

    def _cancel_price_all(self, symbol: str, label: str = "") -> StepResult:
        if symbol:
            self._check_symbol(symbol)
        prefix = self._own_prefix(label)
        own_tag = self._own_scope_tag(label)
        use_own = self.order_scope == "own" and prefix and own_tag not in ("", "signal")
        if use_own:
            symbols = [symbol] if symbol else sorted({
                (p.get("contract") or (p.get("initial") or {}).get("contract") or "")
                for p in (self.client.list_price_orders(symbol) or [])
            } - {""})
            cancelled = []
            errors = []
            for sym in symbols or ([symbol] if symbol else []):
                for p in self._owned_price_orders(sym, prefix):
                    pid = str(p.get("id") or "")
                    if not pid:
                        continue
                    try:
                        self.client.cancel_price_order(pid)
                        cancelled.append(pid)
                    except GateApiError as e:
                        errors.append(f"{pid}: {e}")
            ok = not errors
            return StepResult("cancel_price_all", symbol, ok, detail={
                "cancelled": cancelled, "errors": errors,
                "order_scope": "own", "prefix": prefix,
            }, error="; ".join(errors) if errors else None)
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
            order_type = intent.tp_type or "market"
            limit = intent.tp_limit_price
        else:
            rule = intent.trigger_rule_sl
            order_type = intent.sl_type or "market"
            limit = intent.sl_limit_price

        # size for close-trigger: opposite side contracts; 0 would mean full close —
        # use explicit integer size of the opened contracts.
        close_size = int(size)
        # API: buy to close short (positive), sell to close long (negative)
        api_size = -close_size if (intent.side or "long") == "long" else close_size

        use_market = order_type == "market"
        if use_market:
            # Gate price-trigger: price=0 → market on trigger (guaranteed exit)
            init_price = "0"
            init_tif = "ioc"
        else:
            if limit is None:
                limit = default_trigger_limit_price(float(trigger_price), intent.side or "long", is_tp)
            init_price = str(round_price(float(limit), meta))
            init_tif = "gtc"

        body: dict[str, Any] = {
            "initial": {
                "contract": intent.symbol,
                "size": api_size,
                "price": init_price,
                "tif": init_tif,
                "reduce_only": True,
                "text": f"t-{self._bot_tag(intent.label)}-{'tp' if is_tp else 'sl'}",
            },
            "trigger": {
                "strategy_type": 0,
                "price_type": PRICE_TYPE_MAP.get(intent.trigger_price_type, 0),
                "price": str(round_price(float(trigger_price), meta)),
                "rule": int(rule),
            },
        }
        if intent.trigger_expiration and self.client.env == "live":
            body["trigger"]["expiration"] = int(intent.trigger_expiration)
        if intent.margin_mode:
            body["pos_margin_mode"] = intent.margin_mode

        # Gate price_orders body is nested {initial, trigger} (same as quick_order.cmd_trigger_order)
        return self.client.place_price_order(body)
