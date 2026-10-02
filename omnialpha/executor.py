"""Execute parsed intents against Gate.io."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .gate_client import GateApiError, GateClient, resolve_symbol
from .schema import Intent, SignalFile, expand_signal, infer_trigger_rules
from .sizing import (
    default_trigger_limit_price,
    pct_to_size_usd,
    round_price,
    usd_to_contracts,
    vol_adjust_size,
)

log = logging.getLogger("omnialpha.executor")


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
        alert_store: Optional[Any] = None,
        root: Optional[Path] = None,
        bot_id: str = "",
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
        # bot 隔离：日初权益等状态写 data/bots/<bot_id>/state/（勿用共享目录）
        self.root = Path(root) if root is not None else Path.cwd()
        self.bot_id = str(bot_id or "").strip()
        # bot namespace: order texts always under t-{label_prefix}*; signal labels cannot escape
        self.label_prefix = str(label_prefix or "").strip()
        # P0.4 告警落盘：权益偏离 / 孤儿保护单
        self.alert_store = alert_store

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
                map_note = self._map_open_to_add(intent)
                if map_note:
                    # 不能映射（超暴露上限 / 不同侧 / 无持仓）→ **良性跳过**，不再 break：
                    #  · 算整轮失败会白烧周期并污染失败归档（模拟盘历史 73 笔 POSITION_EXISTS）
                    #  · break 会让同一信号里**后面的 intent 全不执行** —— 例如
                    #    [stop_entry_long, modify_tp_sl] 里第一个被拒，合法的「改保护」也丢了
                    report.results.append(
                        StepResult(intent.action, intent.symbol, True,
                                   detail={"gate_skipped": gate, "map_skipped": map_note})
                    )
                    continue
                # 已映射成同侧 add_*（_entry_gate 对 add_* 放行）→ 继续执行
            try:
                step = self._execute_intent(intent)
            except GateApiError as e:
                step = StepResult(intent.action, intent.symbol, False, error=str(e))
            except Exception as e:  # noqa: BLE001 — boundary
                step = StepResult(intent.action, intent.symbol, False, error=repr(e))
            requested = (intent.meta or {}).get("requested_action")
            if requested and requested != step.action:
                extra = {"executed_as": step.action}
                if (intent.meta or {}).get("mapped_from"):
                    # 显式留痕：这条原本是 open_*，被映射成 add_* 执行了
                    # （复盘「为什么这轮加了仓」时要看得到）
                    extra["mapped_from"] = intent.meta["mapped_from"]
                step = StepResult(
                    action=requested,
                    symbol=step.symbol,
                    ok=step.ok,
                    detail={**step.detail, **extra},
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
                pid = self._order_id(p)
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
                keep_price[intent.symbol].add(self._order_id(p))
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
            "hold", "modify_tp_sl", "close", "close_all", "flatten",
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

    def _map_open_to_add(self, intent: Intent) -> str:
        """有持仓时的**同侧** `open_*` → 视为 `add_*`。返回 '' 表示已映射，否则返回不映射的原因。

        动机：AI 说 `open_long` 而已有同侧持仓时，`_entry_gate` 会拒（注释写明防
        「new plan pile-up」），但 AI 的意图往往是「想更长」—— 拒掉等于白烧一轮
        （实测 `POSITION_EXISTS` 占修复后残留失败的 28%）。

        **必须带暴露上限**：映射成 add 意味着重复/过期计划会**每轮都加仓**，在杠杆下就是
        仓位膨胀（而且单调累积、不会自己回退）。所以只在「当前持仓名义 + 本单名义 ≤
        权益 × `max_notional_pct`」时才映射 —— 用账户自己的暴露策略当上限；超出就退回拒绝，
        防堆积保护重新生效。
        """
        requested = (intent.meta or {}).get("requested_action") or intent.action
        try:
            pos = self._symbol_positions(intent.symbol)
        except Exception:  # noqa: BLE001
            return "无法读取持仓"
        if not pos:
            return "无持仓"
        sides = {p["side"] for p in pos}
        if len(sides) != 1:
            return f"持仓方向不唯一 {sorted(sides)}"
        side = next(iter(sides))
        if requested == f"add_{side}":
            return ""                                   # 本来就是 add
        if requested != f"open_{side}":
            return f"与持仓不同侧（{requested} vs {side}）—— 应先 close/reduce"
        try:
            pct = float((self.account_risk or {}).get("max_notional_pct") or 5.0)
            acct = self.client.get_account() or {}
            equity = float(acct.get("total") or acct.get("balance") or 0)
            meta = self.client.get_contract(intent.symbol)
            quanto = float(getattr(meta, "quanto_multiplier", 0) or 0)
            px = float(self.client.get_last_price(intent.symbol) or 0)
            cur = abs(sum(float(p["size"]) for p in pos)) * quanto * px
            add = intent.size_usd
            if add is None and intent.size is not None:
                add = abs(float(intent.size)) * quanto * px
            add = float(add or 0)
            if equity > 0 and pct > 0 and quanto > 0 and px > 0:
                if (cur + add) > equity * pct:
                    return (f"加仓后名义 {cur + add:.0f} > 权益×{pct:g}={equity * pct:.0f}"
                            "（防仓位膨胀）")
        except Exception:  # noqa: BLE001 — 算不出上限就不映射（保守）
            return "暴露上限无法计算"
        intent.meta = {**(intent.meta or {}),
                       "requested_action": f"add_{side}",
                       "mapped_from": intent.action}
        return ""

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
        if action == "modify_tp_sl":
            return self._modify_tp_sl(intent)
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
        manage = {"hold", "modify_tp_sl", "close", "close_all", "flatten", "cancel_all", "cancel_price_all",
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
            # P0.4：权益偏离告警（不拦截，只落盘）
            try:
                if self.alert_store is not None:
                    acct = self.client.get_account() or {}
                    total = float(acct.get("total") or acct.get("balance") or 0)
                    start = self._day_start_equity(total)
                    self.alert_store.equity_deviation(start, total)
            except Exception:  # noqa: BLE001
                pass
            max_lev = ar.get("max_leverage")
            if max_lev is not None and intent.leverage and int(intent.leverage) > int(max_lev):
                raise GateApiError(
                    f"MAX_LEVERAGE: {intent.leverage} > account max_leverage={max_lev}"
                )
            # total notional: sum open positions + this order
            # 两个闸门取更小者：绝对值 max_total_notional_usd 与**按比例**的
            # max_total_notional_pct（权益 × pct）。后者权益变化时自动跟随 —— 绝对值
            # 做不到这点（实测实盘 max_total_notional_usd=10000 相当于 113× 权益，永不触发，
            # 真正 bind 的只有杠杆上限 50×；加了 open_*→add_* 映射后仓位会单调增长，
            # 所以必须有一道**随权益收紧**的总量闸门）。
            max_total = ar.get("max_total_notional_usd")
            max_total_pct = ar.get("max_total_notional_pct")
            if max_total is not None or max_total_pct is not None:
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
                    cap = float(max_total) if max_total is not None else None
                    pct_note = ""
                    if max_total_pct is not None:
                        eq = float((self.client.get_account() or {}).get("total") or 0)
                        if eq <= 0:
                            raise GateApiError(
                                "MAX_TOTAL_NOTIONAL: cannot read equity for max_total_notional_pct"
                            )
                        pc = eq * float(max_total_pct)
                        cap = pc if cap is None else min(cap, pc)
                        pct_note = f" (权益×{float(max_total_pct):g})"
                    if cap is None:
                        raise GateApiError("MAX_TOTAL_NOTIONAL: no cap configured")
                    if this_usd is not None and pos_notional + float(this_usd) > cap:
                        raise GateApiError(
                            f"MAX_TOTAL_NOTIONAL: open={pos_notional:.2f} + this={this_usd:.2f} "
                            f"> {cap:.0f}{pct_note}"
                        )
                except GateApiError:
                    raise
                except Exception:  # noqa: BLE001 — if cannot measure, do not silently allow huge size
                    raise GateApiError("MAX_TOTAL_NOTIONAL: cannot measure account exposure")

    def _day_start_equity(self, current_total: float) -> float:
        """Persist UTC-day start equity for daily_loss_limit.

        **按 bot 隔离**：写 data/bots/<bot_id>/state/_account_risk/equity_<day>.json。
        所有 bot 共享一个文件会让 DAILY_LOSS_LIMIT 拿别家的日初对比自家权益。
        """
        from datetime import datetime, timezone
        import json as _json

        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        try:
            from .paths import bot_paths

            base = bot_paths(self.root, self.bot_id or "_unknown").state / "_account_risk"
        except Exception:  # noqa: BLE001
            base = Path(self.root) / "data" / "bots" / (self.bot_id or "_unknown") / "state" / "_account_risk"
        path = base / f"equity_{day}.json"
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
        if size_usd is None:
            return
        # 权益比例硬顶：默认单笔名义 ≤ 5×权益（可配 account_risk.max_notional_pct）
        # 注意：紧止损 + 2% 风险会推高名义；0.5× 会误杀正常仓（永续带杠杆）
        pct = self.account_risk.get("max_notional_pct")
        if pct is None:
            pct = 5.0
        try:
            pct = float(pct)
            if pct > 0:
                acct = self.client.get_account() or {}
                equity = float(acct.get("total") or acct.get("balance") or 0)
                if equity > 0:
                    cap_pct = equity * pct
                    if size_usd > cap_pct:
                        raise GateApiError(
                            f"MAX_NOTIONAL_PCT: size_usd={size_usd:.0f} > equity×{pct:.0%}={cap_pct:.0f}"
                        )
        except GateApiError:
            raise
        except Exception:  # noqa: BLE001
            pass
        if self.max_notional_usd is not None and size_usd > self.max_notional_usd:
            raise GateApiError(
                f"size_usd={size_usd} exceeds max_notional_usd={self.max_notional_usd}"
            )

    def _align_size_to_risk(self, intent: Intent, entry: float) -> tuple[float, str]:
        """按风险公式钳制 size_usd：应有 = 权益×risk% ÷ SL距离% × 入场。

        返回 (adjusted_size_usd, note)。无 sl/权益时原样返回。
        """
        size_usd = intent.size_usd
        if size_usd is None or intent.sl is None or entry <= 0:
            return size_usd, ""
        sl = float(intent.sl)
        dist = abs(entry - sl) / entry
        if dist <= 0:
            return size_usd, ""
        risk_pct = self.account_risk.get("risk_pct")
        if risk_pct is None:
            risk_pct = self.account_risk.get("risk_per_trade_pct", 0.01)
        try:
            risk_pct = float(risk_pct)
        except (TypeError, ValueError):
            risk_pct = 0.01
        if risk_pct <= 0:
            return size_usd, ""
        try:
            acct = self.client.get_account() or {}
            equity = float(acct.get("total") or acct.get("balance") or 0)
        except Exception:  # noqa: BLE001
            return size_usd, ""
        if equity <= 0:
            return size_usd, ""
        # dist = |entry-sl|/entry（比例）；应有名义 = 权益×risk% ÷ dist
        expected = equity * risk_pct / dist
        if expected <= 0:
            return size_usd, ""
        # 偏差超过 30% 过大 → 钳到应有值；过小（<50%）保留（可主动降风险）
        if float(size_usd) > expected * 1.3:
            note = f"size_clamped {float(size_usd):.0f}->{expected:.0f} (risk {risk_pct:.1%} expected)"
            return expected, note
        return float(size_usd), ""

    def _atr_pct(self, symbol: str, lookback: int = 14) -> float:
        """近 lookback 根 K 线的 ATR%（=ATR/close×100）。取不到返回 0。"""
        try:
            klines = self.client.get_klines(symbol, "1h", lookback + 1) or []
            if len(klines) < 2:
                return 0.0
            trs = []
            for i in range(1, len(klines)):
                h = float(klines[i].get("h") or klines[i].get("high") or 0)
                l = float(klines[i].get("l") or klines[i].get("low") or 0)
                pc = float(klines[i - 1].get("c") or klines[i - 1].get("close") or 0)
                if h <= 0 or l <= 0:
                    continue
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            if not trs:
                return 0.0
            atr = sum(trs[-lookback:]) / len(trs[-lookback:])
            close = float(klines[-1].get("c") or klines[-1].get("close") or 0)
            if close <= 0:
                return 0.0
            return atr / close * 100.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _vol_adjust(self, intent: Intent, size_usd: float) -> tuple[float, str]:
        """ATR 目标波动缩放（account_risk.vol_target_pct=0 或未配 → 不动）。"""
        target = (self.account_risk or {}).get("vol_target_pct")
        if not target:
            return size_usd, ""
        try:
            target = float(target)
        except (TypeError, ValueError):
            return size_usd, ""
        if target <= 0:
            return size_usd, ""
        atr_pct = self._atr_pct(intent.symbol)
        if atr_pct <= 0:
            return size_usd, ""
        adjusted = vol_adjust_size(float(size_usd), atr_pct, target_pct=target)
        if adjusted == float(size_usd):
            return size_usd, ""
        note = f"vol_adjust {float(size_usd):.0f}->{adjusted:.0f} (atr {atr_pct:.2f}% target {target:.2f}%)"
        return adjusted, note

    # ── actions ──────────────────────────────────────────
    def _precheck_exit_triggers(self, intent: Intent) -> Optional[str]:
        """校验 TP/SL 触发价是否落在参考价（mark）的合法一侧。

        Gate 条件单硬规则：rule=1（涨破触发）要求触发价 > mark，rule=2（跌破触发）
        要求触发价 < mark；否则会立即触发，交易所直接拒（AUTO_TRIGGER_PRICE_GREATE_MARK
        / _LESS_MARK）。而 TP/SL 价位是从**入场价**推出来的：入场价离市价较远时，
        推出来的价位就落在 mark 的非法一侧 —— 那时保护单挂不上，但入场单已经挂在
        交易所上了，等于留下一张**无保护的待成交委托**（一旦成交就是裸仓）。

        所以宁可不交易：任一腿触发价非法就整笔中止，连入场单也不下。
        """
        legs: list[tuple[str, float, int]] = []
        if intent.tp is not None and intent.tp_mode != "limit_order":
            rule_tp = int(intent.trigger_rule_tp or 1)
            for px in (intent.tp, intent.tp2, intent.tp3):
                if px is not None:
                    legs.append(("tp", float(px), rule_tp))
        if intent.sl is not None and intent.sl_mode != "limit_order":
            legs.append(("sl", float(intent.sl), int(intent.trigger_rule_sl or 2)))
        if not legs:
            return None
        try:
            ticker = self.client.get_ticker(intent.symbol) or {}
            ref = float(ticker.get("mark_price") or ticker.get("last") or 0)
        except Exception:  # noqa: BLE001 — 取不到参考价就不拦，保持原行为
            return None
        if ref <= 0:
            return None
        bad = []
        for kind, px, rule in legs:
            if rule == 1 and px <= ref:
                bad.append(f"{kind}={px:g} 需 > mark {ref:g}（rule=1 涨破触发）")
            elif rule == 2 and px >= ref:
                bad.append(f"{kind}={px:g} 需 < mark {ref:g}（rule=2 跌破触发）")
        return ("TRIGGER_PRICE_SIDE: " + "; ".join(bad)) if bad else None

    def _open(self, intent: Intent) -> StepResult:
        self._check_symbol(intent.symbol)
        self._check_open_sl(intent)
        self._check_account_risk(intent)
        meta = self.client.get_contract(intent.symbol)
        entry_for_risk = intent.price if intent.price is not None else self.client.get_last_price(intent.symbol)
        size_note = ""
        if intent.size_usd is not None:
            intent.size_usd, size_note = self._align_size_to_risk(intent, float(entry_for_risk))
            vol_size, vol_note = self._vol_adjust(intent, intent.size_usd)
            intent.size_usd = vol_size
            if vol_note:
                size_note = f"{size_note}; {vol_note}" if size_note else vol_note
        self._check_notional(intent.size_usd)
        # 挂入场单前先验触发价合法性：保护单挂不上时入场单已挂出 = 无保护挂单
        side_err = self._precheck_exit_triggers(intent)
        if side_err:
            return StepResult(intent.action, intent.symbol, False,
                              detail={"precheck": side_err}, error=side_err)

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
            # 通知卡片用：明确暴露入场/止损/止盈价位
            "entry_price": intent.price if intent.price is not None else (
                self.client.get_last_price(intent.symbol) if entry_rec else None
            ),
            "sl": intent.sl,
            "tp": intent.tp,
            "tp2": intent.tp2,
            "quanto_multiplier": meta.quanto_multiplier,
            "leg_entry_ok": entry_rec is not None,
        }
        if size_note:
            detail["size_align_note"] = size_note
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
        # 多级止盈：tp/tp2/tp3（缺省均分或 tp1_share/tp2_share）
        legs = self._tp_legs(intent, exit_size)
        if legs:
            for tp_px, tp_sz, idx in [(p, s, i) for i, (p, s) in enumerate(legs)]:
                rec_, err_ = self._place_exit_leg(
                    lambda p=tp_px, s=tp_sz: (
                        self._place_limit_exit(intent, pos_side, p, is_tp=True, meta=meta, size=s)
                        if intent.tp_mode == "limit_order"
                        else self._place_trigger(intent, trigger_side, p, is_tp=True, meta=meta, size=s)
                    ),
                    kind=f"tp{idx+1}" if len(legs) > 1 else "tp",
                    price_order=(intent.tp_mode != "limit_order"),
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
            detail["rollback"] = self._rollback_unprotected_entry(intent, entry_order)
            return StepResult(
                intent.action,
                intent.symbol,
                False,
                detail=detail,
                error="exit_not_placed: " + "; ".join(str(x) for x in exit_errors or ["tp/sl missing"]),
            )
        return StepResult(intent.action, intent.symbol, True, detail=detail)

    def _rollback_unprotected_entry(self, intent: Intent, entry_order: dict) -> str:
        """exits 挂失败后的兜底：不留无保护敞口。

        - 入场单**未成交** → 撤掉它，回到「什么都没挂」的干净状态（无副作用）
        - 入场单**已成交** → **只告警，不自动平仓**

        为什么已成交不自动平仓：这条路径新写、未在实盘验证过。若因误判而自动
        市价平仓，等于**主动扔掉策略本来想持有的仓位** —— 比原问题更主动、更难
        回滚；而且任何一次挂单失败（网络抖动、5xx、保证金不足…）都会触发平仓，
        爆炸半径过大。交给下一轮 plan（≤ interval）+ 人工处理，同时落盘告警。

        注：预检 `_precheck_exit_triggers` 已挡掉已知成因（触发价落在 mark 非法
        一侧），这里是兜底 —— 比如预检与挂单之间 mark 移动了、或交易所因别的
        原因拒绝。
        """
        oid = str(entry_order.get("id") or "")
        size = abs(int(entry_order.get("size") or 0))
        left = abs(int(entry_order.get("left") or 0))
        filled = size if entry_order.get("finish_as") == "filled" else size - left
        if not oid:
            return "skip: 入场单无 id"
        if filled <= 0:
            try:
                self.client.cancel_order(oid)
                return f"cancelled_entry:{oid}"
            except Exception as e:  # noqa: BLE001
                return f"cancel_failed:{oid}:{str(e)[:90]}"
        if self.alert_store is not None:
            try:
                self.alert_store.raise_alert(
                    "unprotected_entry",
                    f"{intent.symbol} {intent.action} 入场已成交 {filled}/{size}，保护单未挂上，需人工/下一轮 plan 处理",
                    symbol=intent.symbol, action=intent.action,
                    entry_order_id=oid, filled=filled, size=size,
                )
            except Exception:  # noqa: BLE001
                pass
        return f"alert_only: 已成交 {filled}/{size} 但保护单未挂上（未自动平仓）"

    def _tp_legs(self, intent: Intent, exit_size: int) -> list[tuple[float, int]]:
        """多级止盈腿：tp/tp2/tp3 → [(price, size)]。份额缺省 1/N。"""
        prices = [p for p in (intent.tp, intent.tp2, intent.tp3) if p is not None]
        if not prices or exit_size <= 0:
            return []
        if len(prices) == 1:
            return [(prices[0], int(exit_size))]
        s1 = intent.tp1_share
        s2 = intent.tp2_share
        if s1 is None and s2 is None:
            share = [1.0 / len(prices)] * len(prices)
        else:
            s1 = float(s1 if s1 is not None else (1.0 / 3.0))
            s2 = float(s2 if s2 is not None else (1.0 / 3.0))
            s1 = min(1.0, max(0.0, s1))
            s2 = min(1.0 - s1, max(0.0, s2))
            rest = max(0.0, 1.0 - s1 - s2)
            share = [s1, s2] + ([rest] if len(prices) > 2 else [])
            if len(share) < len(prices):
                share.append(0.0)
            if abs(sum(share) - 1.0) > 1e-6 and len(prices) == 2:
                share = [s1, 1.0 - s1]
        legs = []
        acc = 0
        for i, px in enumerate(prices):
            if i < len(prices) - 1:
                sz = max(1, int(round(exit_size * share[i])))
                acc += sz
            else:
                sz = max(1, exit_size - acc)
            legs.append((float(px), int(sz)))
        return legs

    def _modify_tp_sl(self, intent: Intent) -> StepResult:
        """Move TP/SL on an existing position (place new first, then drop old owned).

        Only replaces the kind(s) provided: tp only → keep SL; sl only → keep TP.
        """
        self._check_symbol(intent.symbol)
        if intent.tp is None and intent.sl is None:
            return StepResult(
                "modify_tp_sl", intent.symbol, False,
                error="modify_tp_sl requires tp and/or sl",
            )
        positions = self._symbol_positions(intent.symbol)
        if not positions:
            # 良性 no-op：无持仓就没有保护单可调，**不能算失败** —— 否则整轮被记成
            # failed、白烧一个周期并污染失败归档。实测两种成因都会走到这里：
            #   ① 计划同轮里先 reduce/close 再 modify（模拟盘 103 笔）
            #   ② AI 看到「随未成交入场单预挂的保护单」以为有持仓（实盘 07:59 那轮）
            # 与 gate_client.close_position() 对「已平的账本」按 no-op success 处理一致。
            return StepResult(
                "modify_tp_sl", intent.symbol, True,
                detail={"noop": f"no position on {intent.symbol}; nothing to modify"},
            )
        side = intent.side
        if side in ("long", "short"):
            pos = next((p for p in positions if p["side"] == side), None)
            if not pos:
                return StepResult(
                    "modify_tp_sl", intent.symbol, False,
                    error=f"NO_POSITION: no {side} on {intent.symbol}",
                )
        elif len(positions) == 1:
            pos = positions[0]
            side = pos["side"]
        else:
            return StepResult(
                "modify_tp_sl", intent.symbol, False,
                error="AMBIGUOUS_SIDE: dual position requires side=long|short",
            )
        size = abs(int(pos["size"]))
        meta = self.client.get_contract(intent.symbol)
        pos_side = side
        trigger_side = "short" if pos_side == "long" else "long"
        open_action = "open_long" if pos_side == "long" else "open_short"
        # rule direction depends on position side
        if intent.trigger_rule_tp is None:
            intent.trigger_rule_tp = infer_trigger_rules(open_action, True)
        if intent.trigger_rule_sl is None:
            intent.trigger_rule_sl = infer_trigger_rules(open_action, False)
        # _place_trigger signs by intent.side
        intent.side = pos_side

        prefix = self._own_prefix(intent.label)
        owned = self._owned_price_orders(intent.symbol, prefix) if prefix else []

        def _kind_of(po: dict) -> str:
            text = str((po.get("initial") or {}).get("text") or po.get("text") or "")
            if text.endswith("-tp") or text.endswith("-lp"):
                return "tp"
            if text.endswith("-sl") or text.endswith("-ls"):
                return "sl"
            return ""

        new_ids: set[str] = set()
        detail: dict[str, Any] = {
            "position_side": pos_side,
            "position_size": size,
            "prefix": prefix,
        }
        errors: list[str] = []

        for kind, price, mode in (
            ("tp", intent.tp, intent.tp_mode),
            ("sl", intent.sl, intent.sl_mode),
        ):
            if price is None:
                detail[f"{kind}_skipped"] = True
                continue
            try:
                rec_, err_ = self._place_exit_leg(
                    lambda p=price, m=mode, k=kind: (
                        self._place_limit_exit(intent, pos_side, p, is_tp=(k == "tp"), meta=meta, size=size)
                        if m == "limit_order"
                        else self._place_trigger(
                            intent, trigger_side, p, is_tp=(k == "tp"), meta=meta, size=size
                        )
                    ),
                    kind=kind,
                    price_order=(mode != "limit_order"),
                )
            except Exception as e:  # noqa: BLE001 — boundary
                rec_, err_ = None, str(e)
            if rec_:
                order = rec_.get("order") or {}
                oid = str(order.get("id") or "")
                if oid:
                    new_ids.add(oid)
                detail[f"{kind}_placed"] = {"id": oid, "price": price, "check": rec_.get("check")}
                # 统一字段名：与 open_* 的 detail.tp/detail.sl 对齐，供卡片/日志等消费者直读
                detail[kind] = price
            else:
                errors.append(f"{kind}: {err_}")
                detail[f"{kind}_error"] = str(err_)

        # cancel old owned TP/SL of the kinds we just replaced (never stop_entry)
        cancelled = []
        for po in owned:
            kind = _kind_of(po)
            if not kind or kind not in ("tp", "sl"):
                continue
            # only replace kinds the caller asked to move
            if (kind == "tp" and intent.tp is None) or (kind == "sl" and intent.sl is None):
                continue
            status = str(po.get("status") or "").lower()
            if status in ("cancelled", "finished", "filled", "triggered", "failed", "closed"):
                continue  # already terminal — do not re-cancel
            pid = self._order_id(po)
            if not pid or pid in new_ids:
                continue
            try:
                self.client.cancel_price_order(pid)
                cancelled.append(pid)
            except Exception as e:  # noqa: BLE001
                errors.append(f"cancel {pid}: {e}")
        detail["cancelled_old"] = cancelled

        ok = not errors
        return StepResult(
            "modify_tp_sl",
            intent.symbol,
            ok,
            detail=detail,
            error="; ".join(errors) if errors else "",
        )

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
        # 突破入场也要预挂保护（含双止盈），成交即生效
        if intent.sl is not None or intent.tp is not None:
            contracts_abs = abs(int(contracts))
            pos_side = "long" if intent.action == "stop_entry_long" else "short"
            trigger_side = "short" if pos_side == "long" else "long"
            # stop_entry 用 trigger_price 作入场参考算 SL 规则方向
            saved_side = intent.side
            intent.side = pos_side
            if intent.trigger_rule_sl is None:
                intent.trigger_rule_sl = infer_trigger_rules(
                    "open_long" if pos_side == "long" else "open_short", False
                )
            if intent.trigger_rule_tp is None:
                intent.trigger_rule_tp = infer_trigger_rules(
                    "open_long" if pos_side == "long" else "open_short", True
                )
            tp1_share = float(intent.tp1_share if intent.tp1_share is not None else 0.5)
            tp1_share = min(1.0, max(0.0, tp1_share))
            tps = []
            sls = []
            errs = []
            legs = self._tp_legs(intent, contracts_abs)
            for tp_px, tp_sz, idx in [(p, s, i) for i, (p, s) in enumerate(legs)]:
                rec_, err_ = self._place_exit_leg(
                    lambda p=tp_px, s=tp_sz: self._place_trigger(
                        intent, trigger_side, p, is_tp=True, meta=meta, size=s
                    ),
                    kind=f"tp{idx+1}" if len(legs) > 1 else "tp", price_order=True,
                )
                (tps if rec_ else errs).append(rec_ or err_)
            if intent.sl is not None:
                rec_, err_ = self._place_exit_leg(
                    lambda: self._place_trigger(
                        intent, trigger_side, intent.sl, is_tp=False, meta=meta, size=contracts_abs
                    ),
                    kind="sl", price_order=True,
                )
                (sls if rec_ else errs).append(rec_ or err_)
            intent.side = saved_side
            detail["tp_orders"] = tps
            detail["sl_orders"] = sls
            detail["exit_errors"] = errs
            detail["hang_mode"] = "with_stop_entry"
            if errs:
                return StepResult(intent.action, intent.symbol, False, detail=detail,
                                  error="exit_not_placed: " + "; ".join(str(x) for x in errs))
        return StepResult(intent.action, intent.symbol, True, detail=detail)

    @staticmethod
    def _order_id(po: dict) -> str:
        """Gate 用 `id`，paper 用 `order_id` —— 两个都认。"""
        return str(po.get("id") or po.get("order_id") or po.get("id_string") or "")

    @staticmethod
    def _order_is_reduce_only(po: dict) -> bool:
        """Gate 返回 `initial.is_reduce_only`，paper 侧用 `reduce_only` —— 两个都认。"""
        init = po.get("initial") or {}
        return bool(
            init.get("is_reduce_only")
            or init.get("reduce_only")
            or po.get("is_reduce_only")
            or po.get("reduce_only")
        )

    def _has_pending_entry(self, symbol: str) -> bool:
        """该 symbol 上是否还有本 bot 未成交的入场委托（**普通单 + 条件单**）。

        入场单挂出后 1-3 秒，执行器就把它的 TP/SL 一起挂上了 —— 此时账户还没有持仓。
        若按「无持仓」把这些保护单当孤儿撤掉，委托一成交就是**裸仓**（无 SL/TP）。
        所以只要有待成交入场单，本 symbol 的保护单一律保留；真孤儿留到下一轮扫描再清。

        **必须同时扫条件单**：`stop_entry_*`（突破进场）是**条件单**，挂在
        `/price_orders` 而不是 `/orders`。只扫普通单会漏掉它们 —— 线上实测
        2026-10-02 16:12 / 16:17 / 16:28 连续三次把突破进场单的保护单当孤儿撤掉，
        而当时 mark 距触发只差 0.17%。
        """
        # 1) 普通挂单（limit 未成交的入场单）
        try:
            rows = self._owned_open_orders(symbol, self.label_prefix)
        except Exception:  # noqa: BLE001 — 取不到就当没有，保持原行为
            rows = []
        for o in rows:
            if o.get("is_reduce_only"):
                continue
            if int(o.get("left") or 0) <= 0:
                continue
            return True
        # 2) 条件单（stop_entry_* 突破进场，未触发）
        try:
            conds = self._owned_price_orders(symbol, self.label_prefix)
        except Exception:  # noqa: BLE001
            conds = []
        for p in conds:
            if self._order_is_reduce_only(p):
                continue  # reduce_only = 保护单，不是入场单
            status = str(p.get("status") or "").lower()
            if status in ("cancelled", "finished", "filled", "triggered", "failed", "closed"):
                continue  # 已终结
            return True
        return False

    def _is_orphan_protector(self, po: dict, positions: list) -> bool:
        """判断保护单是否孤儿（无对应持仓）。

        保护单方向：sell(-size) 平多单，buy(+size) 平空单。
        有对应持仓 → 当前计划保护单，保留；无 → 孤儿。
        """
        init = po.get("initial") or {}
        text = str(init.get("text") or po.get("text") or "")
        tail = text.rsplit("-", 1)[-1].lower() if text else ""
        if tail not in ("tp", "sl", "lp", "ls"):
            return False
        if not self._order_is_reduce_only(po):
            return False
        sz = float(init.get("size") or po.get("size") or 0)
        # 保护单方向：负=卖平多，正=买平空
        want = "long" if sz < 0 else "short" if sz > 0 else None
        if want is None:
            return True  # 无方向无法匹配 → 保守当孤儿
        for p in positions:
            psz = float(p.get("size") or 0)
            if psz == 0:
                continue
            side = "long" if psz > 0 else "short"
            if side == want:
                return False  # 有对应持仓 → 不是孤儿
        return True

    def _cleanup_orphan_protectors(self, symbol: str, keep_ids: Optional[set] = None) -> list:
        """回收孤儿保护单（有对应持仓的保留）。

        安全闸：reduce_only + tp/sl 后缀 + 本 bot 命名空间 + 非 keep + **无对应持仓** + **无待成交入场单**。
        """
        if not symbol:
            return []
        keep = {str(x) for x in (keep_ids or set())}
        # 有待成交入场单 → 预挂的 TP/SL 不是孤儿，撤了会裸仓（symbol 级判定）
        if self._has_pending_entry(symbol):
            return []
        try:
            positions = self._symbol_positions(symbol)
            rows = self.client.list_price_orders(symbol) or []
        except Exception:  # noqa: BLE001
            return []
        cancelled = []
        for p in rows:
            init = p.get("initial") or {}
            text = str(init.get("text") or p.get("text") or "")
            tail = text.rsplit("-", 1)[-1].lower() if text else ""
            if tail not in ("tp", "sl", "lp", "ls"):
                continue
            if not self._order_is_reduce_only(p):
                continue
            status = str(p.get("status") or "").lower()
            if status in ("cancelled", "finished", "filled", "triggered", "failed", "closed"):
                continue
            pid = self._order_id(p)
            if not pid or pid in keep:
                continue
            if self.label_prefix:
                if not self._text_owned(text, self.label_prefix):
                    continue
            else:
                continue
            if not self._is_orphan_protector(p, positions):
                continue  # 当前计划保护单，保留
            try:
                self.client.cancel_price_order(pid)
                cancelled.append(pid)
            except Exception:  # noqa: BLE001
                pass
        # P0.4：发现并回收孤儿保护单 → 落盘告警
        if cancelled and self.alert_store is not None:
            try:
                self.alert_store.orphan(symbol, len(cancelled), cancelled)
            except Exception:  # noqa: BLE001
                pass
        return cancelled

    def _resync_protectors(self, symbol: str) -> list:
        """减仓/调仓后把保护单张数同步到剩余持仓。

        - flat：交给 _cleanup_orphan_protectors
        - 有仓：|保护单size| != 持仓size → 撤旧、按现价重挂同 trigger 的等量保护单
        返回动作摘要列表。
        """
        notes: list = []
        if not symbol:
            return notes
        positions = self._symbol_positions(symbol)
        if not positions:
            return notes
        total = sum(abs(float(p.get("size") or 0)) for p in positions)
        if total <= 0:
            return notes
        try:
            rows = self.client.list_price_orders(symbol) or []
        except Exception:  # noqa: BLE001
            return notes
        for p in rows:
            init = p.get("initial") or {}
            text = str(init.get("text") or p.get("text") or "")
            tail = text.rsplit("-", 1)[-1].lower() if text else ""
            if tail not in ("tp", "sl", "lp", "ls"):
                continue
            if not self._order_is_reduce_only(p):
                continue
            status = str(p.get("status") or "").lower()
            if status in ("cancelled", "finished", "filled", "triggered", "failed", "closed"):
                continue
            if self.label_prefix and not self._text_owned(text, self.label_prefix):
                continue
            try:
                psz = float(init.get("size") or p.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if abs(psz) == total:
                continue
            pid = self._order_id(p)
            trig = p.get("trigger") or {}
            trigger_price = trig.get("price") or init.get("trigger_price") or p.get("trigger_price")
            if not trigger_price:
                continue
            # 同向：多单保护为负 size，空单为正
            new_size = -abs(int(total)) if psz < 0 else abs(int(total))
            try:
                if pid:
                    self.client.cancel_price_order(pid)
            except Exception:  # noqa: BLE001
                pass
            # 按原 trigger 重挂
            try:
                side = "buy" if new_size > 0 else "sell"
                body = {
                    "initial": {
                        "contract": symbol,
                        "size": new_size,
                        "price": "0",
                        "tif": "ioc",
                        "reduce_only": True,
                        "text": text or (f"t-{self.label_prefix}-sl" if tail == "sl" else f"t-{self.label_prefix}-tp"),
                    },
                    "trigger": {
                        "rule": int(trig.get("rule") or (2 if psz < 0 else 1)),
                        "price_type": int(trig.get("price_type") or 0),
                        "price": str(trigger_price),
                    },
                }
                self.client.place_price_order(body)
                notes.append({"resized": text or tail, "from": psz, "to": new_size, "trigger": trigger_price})
            except Exception as e:  # noqa: BLE001
                notes.append({"resized_error": str(e), "text": text})
        return notes

    def _has_owned_sl(self, symbol: str, prefix: str) -> bool:
        """是否已有本 bot 的**未终结** SL 保护单。

        只看 SL、不看 TP —— `watcher.reconcile_protection()` 的判据是
        `"-sl" in text or "-tp" in text`，也就是「只有 TP 没有 SL」会被判成已受保护。
        补保护这条路必须按 SL 单独判，不能沿用那个判据。
        """
        for po in self._owned_price_orders(symbol, prefix):
            text = str((po.get("initial") or {}).get("text") or po.get("text") or "")
            tail = text.rsplit("-", 1)[-1].lower() if text else ""
            if tail not in ("sl", "ls"):
                continue
            if str(po.get("status") or "").lower() in (
                "cancelled", "finished", "filled", "triggered", "failed", "closed"
            ):
                continue
            return True
        return False

    def ensure_protection(self, symbol: str) -> dict:
        """裸仓兜底：持仓在、owned SL 不在时，按配置补一张 reduce_only SL。

        生产里此前**没有任何代码会补** —— `watcher.reconcile_protection()` 只返回警告
        （且全仓唯一调用者是上线前测试脚本），`_orphan_sweep` 只撤孤儿不补。于是两条
        路径都会留下裸仓，只能等 AI 下一轮自己发现：
          ① 入场成交了但保护单没挂上（`_rollback_unprotected_entry` 只告警不平仓）
          ② SL 被触发/被撤销，而仓位还在

        只补 SL，**绝不补 TP**：SL 是风控，TP 是策略 —— 自动塞一个 TP 等于替 AI 做了
        它没做的决策，还会和它下一轮的意图打架。

        由 `account_risk` 逐 bot 开启（默认关）：
          auto_protect: false（默认）→ 只回报检测结果，不下单
          auto_protect: "dry"        → 记录「本来会挂什么」，不下单
          auto_protect: true         → 真挂
          auto_protect_sl_pct: 2.0   → SL 距 mark 的百分比；≤0 视为未启用

        价格锚 **mark**（不锚入场价）：仓位已经很赚时按入场价算出来的价会落在 mark 的
        非法一侧、被交易所直接拒 —— 正是 `_precheck_exit_triggers` 挡的那一类。这里再
        走一次同一个预检，避免两处判据漂移。

        本方法只做「检测 + 挂单」，**不告警**：告警与限流是调用方（watcher 的扫描循环）
        的策略，那里才有跨轮状态。
        """
        out: dict[str, Any] = {"symbol": symbol, "auto_protect": "off"}
        ar = self.account_risk or {}
        mode = ar.get("auto_protect", False)
        try:
            pct = float(ar.get("auto_protect_sl_pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if not mode or pct <= 0:
            return out
        dry = str(mode).lower() == "dry"
        out["auto_protect"] = "dry" if dry else "live"

        positions = self._symbol_positions(symbol)
        if not positions:
            out["skipped"] = "no_position"
            return out
        if len(positions) > 1:
            # 双向持仓得先定补哪条腿 —— 这不是「兜底」该猜的事，交给 AI
            out["skipped"] = "ambiguous_side"
            return out
        if self._has_pending_entry(symbol):
            # 执行器已为未成交入场单预挂了保护单，此刻再补一张会重复。
            # 与孤儿扫描共用同一个事实源，避免两处判据漂移。
            out["skipped"] = "pending_entry"
            return out

        pos = positions[0]
        pos_side = pos["side"]
        size = abs(int(pos["size"]))
        if self._has_owned_sl(symbol, self._own_prefix("")):
            out["skipped"] = "sl_present"
            return out

        try:
            ticker = self.client.get_ticker(symbol) or {}
            mark = float(ticker.get("mark_price") or ticker.get("last") or 0)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"no mark price: {e}"
            return out
        if mark <= 0:
            out["error"] = "no mark price"
            return out

        meta = self.client.get_contract(symbol)
        raw = mark * (1 - pct / 100.0) if pos_side == "long" else mark * (1 + pct / 100.0)
        sl = float(round_price(raw, meta))
        out.update({
            "position_side": pos_side, "position_size": size,
            "mark": mark, "sl": sl, "sl_pct": pct,
        })
        if dry:
            return out

        intent = Intent(
            action="open_long" if pos_side == "long" else "open_short",
            symbol=symbol, side=pos_side, sl=sl,
            sl_type="market",  # 触发即市价：兜底止损要的是「一定出得来」
            label=self.label_prefix or "auto",
        )
        intent.trigger_rule_sl = infer_trigger_rules(intent.action, False)
        side_err = self._precheck_exit_triggers(intent)
        if side_err:
            out["error"] = side_err
            return out

        trigger_side = "short" if pos_side == "long" else "long"
        try:
            rec, err = self._place_exit_leg(
                lambda: self._place_trigger(
                    intent, trigger_side, sl, is_tp=False, meta=meta, size=size
                ),
                kind="sl",
                price_order=True,
            )
        except Exception as e:  # noqa: BLE001 — boundary
            rec, err = None, str(e)
        if not rec:
            out["error"] = f"place failed: {err}"
            return out
        order = rec.get("order") or {}
        out["placed"] = {"id": str(order.get("id") or ""), "price": sl, "check": rec.get("check")}
        return out

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
        # 平/减仓后：先撤孤儿，再把剩余保护单张数对齐持仓
        cleaned = self._cleanup_orphan_protectors(intent.symbol)
        resized = self._resync_protectors(intent.symbol)
        # 统一字段名：与 open_* 的 detail 对齐，供卡片/日志等消费者直读
        _o = order or {}
        _px = _o.get("fill_price") or _o.get("avg_price") or _o.get("price")
        _pnl = _o.get("pnl")
        detail: dict[str, Any] = {
            "order": order,
            "side": side,
            "position_mode": self.client.get_position_mode(),
            "executed_as": "close",
            "cleaned_protectors": cleaned,
            "resized_protectors": resized,
            "mode_note": (
                "dual: close this side only" if dual else "single: one book, side is advisory"
            ),
        }
        if _px not in (None, "", "0"):
            detail["entry_price"] = _px
        if _pnl is not None:
            detail["realized_pnl"] = _pnl
        return StepResult(
            requested,
            intent.symbol,
            True,
            detail=detail,
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
                    pid = self._order_id(p)
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
