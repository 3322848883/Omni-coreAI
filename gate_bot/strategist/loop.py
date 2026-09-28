"""Strategist loop: snapshot → LLM → risk → write inbox.

Triggers (per-bot, configurable):
  - interval_sec 定时（5m/10m/… 任意秒）
  - event_on_kline_close + event_timeframe K 线收盘
  - conditions[]：EMA/ATR/RSI/价格突破等条件事件
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..gate_client import GateClient
from .bridge import chips_to_signal, write_hold_audit, write_signal_file
from .llm_client import LLMClient, LLMConfig, LLMError
from .market import MarketConfig
from .prompt import build_system_prompt, build_user_prompt, load_strategy_prompt
from .risk import RiskConfig, apply_risk
from .schema import PlanError, parse_plan_text
from .snapshot import collect_snapshot
from .tools import NATIVE_TOOLS, TOOL_GUIDE, extract_tool_calls, run_tool
from .triggers import check_conditions, parse_conditions
from .trigger_store import AITriggerPolicy, AITriggerStore, TriggerPolicyError, validate_trigger_payload

log = logging.getLogger("gate_bot.strategist")


@dataclass
class StrategistConfig:
    enabled: bool = True
    interval_sec: int = 300
    timeframe: str = "15m"
    event_on_kline_close: bool = True
    # K 收盘 / 条件事件用的周期；默认跟 timeframe
    event_timeframe: Optional[str] = None
    # 条件事件：EMA/ATR/RSI/price_break 等（见 triggers.py）
    conditions: list = field(default_factory=list)
    check_interval_sec: float = 1.0
    ai_triggers: dict = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    prompt_file: str = "prompts/vergex_default.md"
    write_hold: bool = True
    candles: int = 60
    market: MarketConfig = field(default_factory=MarketConfig)
    env: str = "live"
    bot_root: Optional[Path] = None
    risk: RiskConfig = field(default_factory=RiskConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    # on-demand market tools for the LLM (not a full dump)
    tools: dict = field(default_factory=dict)
    bot_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_timeframe:
            self.event_timeframe = self.timeframe


class PlanRunner:
    def __init__(
        self,
        client: GateClient,
        cfg: StrategistConfig,
        inbox: Path,
        history_dir: Path,
        llm: Optional[LLMClient] = None,
    ):
        self.client = client
        self.cfg = cfg
        self.inbox = inbox
        self.history_dir = history_dir
        self.llm = llm or LLMClient(cfg.llm)
        self.strategy_prompt = load_strategy_prompt(
            cfg.prompt_file or None,
            bot_root=cfg.bot_root,
        )
        self._last_kline_t: Optional[int] = None
        self._last_cycle_id: Optional[str] = None
        self._busy = threading.Lock()
        self._conditions = parse_conditions(cfg.conditions)
        self._cond_states: dict[str, Any] = {}
        self._last_trigger: str = ""
        self._cycle_file = history_dir / "last_cycle.json"
        self._last_cycle_id = self._load_last_cycle()
        self._ai_policy = AITriggerPolicy(
            enabled=bool((cfg.ai_triggers or {}).get("enabled")),
            allow_types=tuple((cfg.ai_triggers or {}).get("allow_types") or AITriggerPolicy.__dataclass_fields__["allow_types"].default),
            max_active=int((cfg.ai_triggers or {}).get("max_active") or 5),
            default_cooldown_sec=float((cfg.ai_triggers or {}).get("default_cooldown_sec") or 60),
            default_ttl_sec=float((cfg.ai_triggers or {}).get("default_ttl_sec") or 86400),
            allow_modify=bool((cfg.ai_triggers or {}).get("allow_modify", True)),
        )
        self._ai_store = AITriggerStore(self.history_dir / "ai_triggers.json", self._ai_policy)

    def _load_last_cycle(self) -> Optional[str]:
        try:
            if self._cycle_file.exists():
                data = json.loads(self._cycle_file.read_text(encoding="utf-8"))
                return data.get("cycle_id")
        except Exception:  # noqa: BLE001
            return None
        return None

    def _save_last_cycle(self, cycle_id: str) -> None:
        try:
            self._cycle_file.parent.mkdir(parents=True, exist_ok=True)
            self._cycle_file.write_text(
                json.dumps({"cycle_id": cycle_id, "ts": datetime.now(timezone.utc).isoformat()}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def run_once(self, trigger: str = "manual") -> dict[str, Any]:
        cycle_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot = collect_snapshot(
            self.client,
            self.cfg.symbols,
            candles=self.cfg.candles,
            interval=self.cfg.timeframe,
            market_cfg=self.cfg.market,
            env=self.cfg.env,
            bot_root=self.cfg.bot_root,
        )
        account = snapshot.get("account") or {}
        if account.get("error"):
            return {
                "ok": False,
                "cycle_id": cycle_id,
                "error": "account_unavailable",
                "detail": account.get("error"),
                "trigger": trigger,
            }
        system = build_system_prompt(
            self.strategy_prompt,
            tools_guide=TOOL_GUIDE if self.cfg.tools.get("enabled") else "",
        )
        user = build_user_prompt(
            snapshot,
            {
                "min_confidence": self.cfg.risk.min_confidence,
                "max_notional_usd": self.cfg.risk.max_notional_usd,
                "max_chips": self.cfg.risk.max_chips,
                "allow_actions": sorted(self.cfg.risk.allow_actions)
                if self.cfg.risk.allow_actions
                else None,
            },
            self.cfg.symbols,
        )
        text = self._chat_with_tools(system, user)
        try:
            self._save_thinking(
                cycle_id=cycle_id,
                trigger=trigger,
                content=text,
                reasoning=list(getattr(self.llm, "last_reasoning_chain", []) or []),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            plan = parse_plan_text(text)
        except PlanError as e:
            log.error("plan parse failed: %s", e)
            return {"ok": False, "cycle_id": cycle_id, "error": str(e), "trigger": trigger}
        if not plan.cycle_id:
            plan.cycle_id = cycle_id
        if plan.cycle_id == self._last_cycle_id:
            return {
                "ok": True,
                "cycle_id": plan.cycle_id,
                "skipped": "duplicate_cycle",
                "orders": 0,
                "trigger": trigger,
            }
        self._last_cycle_id = plan.cycle_id
        self._save_last_cycle(plan.cycle_id)
        rejected_triggers = self._apply_ai_triggers(plan)
        risk_result = apply_risk(plan, self.cfg.risk)
        payload = chips_to_signal(plan, risk_result, bot_id=self.cfg.bot_id or self.inbox.name)
        orders = payload.get("orders") or []
        if not orders:
            if self.cfg.write_hold:
                write_hold_audit(self.history_dir, plan, cycle_id=plan.cycle_id)
            self._record_plan(cycle_id=plan.cycle_id, trigger=trigger, orders=0,
                              notes=risk_result.notes + rejected_triggers,
                              reasoning=plan.reasoning)
            return {
                "ok": True,
                "cycle_id": plan.cycle_id,
                "orders": 0,
                "notes": risk_result.notes + rejected_triggers,
                "rejected": len(risk_result.rejected),
                "trigger": trigger,
            }
        path = write_signal_file(self.inbox, payload, cycle_id=plan.cycle_id)
        self._record_plan(cycle_id=plan.cycle_id, trigger=trigger, orders=len(orders),
                          notes=risk_result.notes + rejected_triggers, reasoning=plan.reasoning)
        return {
            "ok": True,
            "cycle_id": plan.cycle_id,
            "orders": len(orders),
            "file": str(path),
            "notes": risk_result.notes + rejected_triggers,
            "rejected": len(risk_result.rejected),
            "trigger": trigger,
        }


    def analyze_once(self, trigger: str = "manual") -> dict[str, Any]:
        """各人格独立分析：LLM → Plan，不写 inbox、不执行（供多人格融合）。"""
        cycle_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot = collect_snapshot(
            self.client,
            self.cfg.symbols,
            candles=self.cfg.candles,
            interval=self.cfg.timeframe,
            market_cfg=self.cfg.market,
            env=self.cfg.env,
            bot_root=self.cfg.bot_root,
        )
        account = snapshot.get("account") or {}
        if account.get("error"):
            return {"ok": False, "cycle_id": cycle_id, "error": "account_unavailable",
                    "detail": account.get("error"), "trigger": trigger}
        system = build_system_prompt(
            self.strategy_prompt,
            tools_guide=TOOL_GUIDE if self.cfg.tools.get("enabled") else "",
        )
        user = build_user_prompt(
            snapshot,
            {
                "min_confidence": self.cfg.risk.min_confidence,
                "max_notional_usd": self.cfg.risk.max_notional_usd,
                "max_chips": self.cfg.risk.max_chips,
                "allow_actions": sorted(
                    self.cfg.risk.allow_actions
                ) if self.cfg.risk.allow_actions else None,
            },
            self.cfg.symbols,
        )
        try:
            text = self._chat_with_tools(system, user)
            self._save_thinking(
                cycle_id=cycle_id, system=system, user=user, text=text, trigger=trigger
            )
        except Exception as e:  # noqa: BLE001
            log.error("analyze llm failed: %s", e)
            return {"ok": False, "cycle_id": cycle_id, "error": str(e), "trigger": trigger}
        try:
            plan = parse_plan_text(text)
        except PlanError as e:
            log.error("analyze plan parse failed: %s", e)
            return {"ok": False, "cycle_id": cycle_id, "error": str(e), "trigger": trigger}
        if not plan.cycle_id:
            plan.cycle_id = cycle_id
        # 给融合层的紧凑 Plan（不写 inbox）
        chips_out = []
        for c in getattr(plan, "chips", []) or []:
            chips_out.append({
                "symbol": getattr(c, "symbol", ""),
                "action": getattr(c, "action", ""),
                "confidence": getattr(c, "confidence", 0.0),
                "size_usd": getattr(c, "size_usd", None),
                "tp": getattr(c, "tp", None),
                "sl": getattr(c, "sl", None),
                "reasoning": getattr(c, "reasoning", ""),
            })
        return {
            "ok": True,
            "cycle_id": plan.cycle_id,
            "trigger": trigger,
            "plan": {
                "cycle_id": plan.cycle_id,
                "reasoning": plan.reasoning,
                "chips": chips_out,
                "decision": chips_out[0]["action"] if chips_out else "hold",
                "confidence": chips_out[0]["confidence"] if chips_out else 0.0,
            },
        }

    def _save_thinking(self, **kw: Any) -> None:
        """Persist LLM reasoning_content (CoT) for audit/replay."""
        from datetime import datetime

        out = self.history_dir
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        cid = str(kw.get("cycle_id") or "cycle").replace(":", "").replace("/", "-")[:40]
        path = out / f"{ts}-{cid}.thinking.json"
        payload = {
            "ts": time.time(),
            "cycle_id": kw.get("cycle_id"),
            "trigger": kw.get("trigger"),
            "reasoning_chain": kw.get("reasoning") or [],
            "content_head": (kw.get("content") or "")[:500],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # also into sqlite ledger
        try:
            from ..ledger import Ledger, default_ledger_path

            root = self.cfg.bot_root or Path.cwd()
            led = Ledger(default_ledger_path(root))
            led.insert_plan(
                self.inbox.name,
                cycle_id=kw.get("cycle_id"),
                trigger=kw.get("trigger"),
                orders=None,
                notes=["thinking_saved:" + path.name],
                reasoning=" | ".join((kw.get("reasoning") or [""])[:1])[:200],
                raw={"thinking_file": path.name,
                     "reasoning_chain": kw.get("reasoning") or [],
                     "content_head": (kw.get("content") or "")[:200]},
            )
            led.close()
        except Exception:  # noqa: BLE001
            pass

    def _record_plan(self, **kw: Any) -> None:
        try:
            from ..ledger import Ledger, default_ledger_path

            root = self.cfg.bot_root or Path.cwd()
            led = Ledger(default_ledger_path(root))
            led.insert_plan(self.inbox.name, **kw)
            led.close()
        except Exception:  # noqa: BLE001
            pass

    def _llm_send(self, messages: list) -> str:
        if hasattr(self.llm, "chat_messages"):
            return self.llm.chat_messages(messages)
        # test doubles / older clients: only chat(system, user)
        system = messages[0]["content"] if messages else ""
        user = "\n\n".join(
            m.get("content") or "" for m in messages[1:] if m.get("role") == "user"
        )
        return self.llm.chat(system, user)

    def _chat_with_tools(self, system: str, user: str) -> str:
        """Official function-calling loop; falls back to text tool_calls JSON."""
        if hasattr(self.llm, "last_reasoning_chain"):
            self.llm.last_reasoning_chain = []
        tools_on = bool(self.cfg.tools.get("enabled"))
        max_rounds = int(self.cfg.tools.get("max_rounds") or 3)
        native = bool(self.cfg.tools.get("native", True))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if not tools_on:
            return self._llm_send(messages)
        if native and hasattr(self.llm, "chat_message_full"):
            return self._chat_native_tools(messages, max_rounds)
        text = self._llm_send(messages)
        for _round in range(max(1, max_rounds)):
            calls = extract_tool_calls(text)
            if not calls:
                return text
            results = []
            for c in calls[:8]:
                name = c.get("tool") or c.get("name") or ""
                args = c.get("args") or c.get("arguments") or {}
                results.append({"tool": name, "result": run_tool(
                    self.client, name, args,
                    env=self.cfg.env, bot_root=self.cfg.bot_root, market_cfg=self.cfg.market,
                )})
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": "【工具结果】\n" + json.dumps(results, ensure_ascii=False)
                + "\n\n若信息足够，请只输出最终 Plan JSON；仍需数据则继续 tool_calls。",
            })
            text = self._llm_send(messages)
        return text

    def _chat_native_tools(self, messages: list, max_rounds: int) -> str:
        """DeepSeek function calling + thinking: must echo reasoning_content on tool turns."""
        for _round in range(max(1, max_rounds) + 1):
            msg = self.llm.chat_message_full(messages, tools=NATIVE_TOOLS, tool_choice="auto")
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or ""
            # official: append full assistant message incl. reasoning_content
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "reasoning_content": msg.get("reasoning_content") or "",
                "tool_calls": calls,
            })
            for tc in calls[:8]:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:  # noqa: BLE001
                    args = {}
                result = run_tool(
                    self.client, name, args,
                    env=self.cfg.env, bot_root=self.cfg.bot_root, market_cfg=self.cfg.market,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": json.dumps(result, ensure_ascii=False),
                })
        # force final without tools
        messages.append({
            "role": "user",
            "content": "工具结果已足够。禁止再调用工具，禁止使用特殊标记。"
            "请只输出最终 Plan JSON 对象。",
        })
        msg = self.llm.chat_message_full(messages)
        return msg.get("content") or ""

    def _kline_closed(self) -> bool:
        """True when latest candle timestamp for first symbol advances."""
        if not self.cfg.symbols:
            return False
        sym = self.cfg.symbols[0]
        interval = self.cfg.event_timeframe or self.cfg.timeframe
        try:
            raw = self.client.public_get(
                "/api/v4/futures/usdt/candlesticks",
                f"contract={sym}&interval={interval}&limit=1",
            )
            if not raw:
                return False
            row = raw[-1]
            ts = int(row[0]) if isinstance(row, (list, tuple)) else int(row.get("t") or 0)
            closed = self._last_kline_t is not None and ts > self._last_kline_t
            self._last_kline_t = ts
            return closed
        except Exception:  # noqa: BLE001
            return False

    def _apply_ai_triggers(self, plan) -> list:
        """Apply Plan.triggers / trigger_ops under ai_triggers policy."""
        notes = []
        if not getattr(self, "_ai_policy", None) or not self._ai_policy.enabled:
            return notes
        ops = plan.trigger_ops or []
        for op in ops:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op") or "").lower()
            try:
                if kind == "remove":
                    self._ai_store.remove(str(op.get("id") or ""))
                elif kind == "replace_all":
                    norms = []
                    for raw in plan.triggers or []:
                        norms.append(validate_trigger_payload(raw, self._ai_policy, self.cfg.symbols))
                    self._ai_store.replace_all(norms, self.cfg.symbols)
                    notes.append(f"ai_triggers: replaced {len(norms)}")
            except TriggerPolicyError as e:
                notes.append(f"trigger_op_rejected: {e}")
        if not ops or any(str(op.get("op") or "").lower() == "add" for op in ops if isinstance(op, dict)):
            for raw in plan.triggers or []:
                try:
                    norm = validate_trigger_payload(raw, self._ai_policy, self.cfg.symbols)
                    t = self._ai_store.add(norm, self.cfg.symbols)
                    notes.append(f"ai_trigger_add: {t.id} {t.type} {t.symbol}")
                except TriggerPolicyError as e:
                    notes.append(f"trigger_rejected: {e}")
        return notes

    def _check_condition_events(self) -> Optional[str]:
        ai_conds = []
        try:
            ai_conds = [t.to_condition() for t in self._ai_store.active()] if getattr(self, "_ai_store", None) else []
        except Exception:  # noqa: BLE001
            ai_conds = []
        all_conds = list(self._conditions) + ai_conds
        if not all_conds:
            return None
        interval = self.cfg.event_timeframe or self.cfg.timeframe
        fired = check_conditions(
            self.client, all_conds, interval, states=self._cond_states
        )
        if not fired:
            return None
        reasons = "; ".join(f"{f['type']}:{f['symbol']}:{f['reason']}" for f in fired)
        log.info("condition fired: %s", reasons)
        return f"cond[{reasons}]"

    def run_forever(self) -> None:
        last_interval = 0.0
        while True:
            now = time.time()
            fire = False
            trigger = ""
            if self.cfg.interval_sec and now - last_interval >= self.cfg.interval_sec:
                fire, trigger = True, "interval"
                last_interval = now
            elif self.cfg.event_on_kline_close and self._kline_closed():
                fire, trigger = True, "kline_close"
            else:
                cond = self._check_condition_events()
                if cond:
                    fire, trigger = True, cond
            if fire:
                if not self._busy.acquire(blocking=False):
                    continue
                try:
                    result = self.run_once(trigger=trigger)
                    log.info("plan[%s] %s", trigger, result)
                except LLMError as e:
                    log.warning("plan[%s] llm error: %s", trigger, e)
                except Exception as e:  # noqa: BLE001
                    log.exception("plan[%s] failed: %s", trigger, e)
                finally:
                    self._busy.release()
            time.sleep(self.cfg.check_interval_sec or 1.0)
