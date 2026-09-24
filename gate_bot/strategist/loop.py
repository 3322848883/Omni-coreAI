"""Strategist loop: snapshot → LLM → risk → write inbox (interval + kline-close)."""
from __future__ import annotations

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
from .prompt import build_system_prompt, build_user_prompt, load_strategy_prompt
from .risk import RiskConfig, apply_risk
from .schema import PlanError, parse_plan_text
from .snapshot import collect_snapshot

log = logging.getLogger("gate_bot.strategist")


@dataclass
class StrategistConfig:
    enabled: bool = True
    interval_sec: int = 300
    timeframe: str = "15m"
    event_on_kline_close: bool = True
    symbols: list[str] = field(default_factory=list)
    prompt_file: str = "prompts/vergex_default.md"
    write_hold: bool = True
    candles: int = 60
    risk: RiskConfig = field(default_factory=RiskConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


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
        self.strategy_prompt = load_strategy_prompt(cfg.prompt_file or None)
        self._last_kline_t: Optional[int] = None
        self._busy = threading.Lock()

    def run_once(self) -> dict[str, Any]:
        cycle_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot = collect_snapshot(
            self.client, self.cfg.symbols, candles=self.cfg.candles, interval=self.cfg.timeframe
        )
        system = build_system_prompt(self.strategy_prompt)
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
        text = self.llm.chat(system, user)
        try:
            plan = parse_plan_text(text)
        except PlanError as e:
            log.error("plan parse failed: %s", e)
            return {"ok": False, "cycle_id": cycle_id, "error": str(e)}
        if not plan.cycle_id:
            plan.cycle_id = cycle_id
        risk_result = apply_risk(plan, self.cfg.risk)
        payload = chips_to_signal(plan, risk_result, bot_id=self.inbox.name)
        orders = payload.get("orders") or []
        if not orders:
            if self.cfg.write_hold:
                write_hold_audit(self.history_dir, plan, cycle_id=plan.cycle_id)
            return {
                "ok": True,
                "cycle_id": plan.cycle_id,
                "orders": 0,
                "notes": risk_result.notes,
                "rejected": len(risk_result.rejected),
            }
        path = write_signal_file(self.inbox, payload, cycle_id=plan.cycle_id)
        return {
            "ok": True,
            "cycle_id": plan.cycle_id,
            "orders": len(orders),
            "file": str(path),
            "notes": risk_result.notes,
            "rejected": len(risk_result.rejected),
        }

    def _kline_closed(self) -> bool:
        """True when latest candle timestamp for first symbol advances."""
        if not self.cfg.symbols:
            return False
        sym = self.cfg.symbols[0]
        try:
            raw = self.client.public_get(
                "/api/v4/futures/usdt/candlesticks",
                f"contract={sym}&interval={self.cfg.timeframe}&limit=1",
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

    def run_forever(self) -> None:
        last_interval = 0.0
        while True:
            now = time.time()
            fire = False
            trigger = ""
            if now - last_interval >= self.cfg.interval_sec:
                fire, trigger = True, "interval"
                last_interval = now
            elif self.cfg.event_on_kline_close and self._kline_closed():
                fire, trigger = True, "kline_close"
            if fire:
                if not self._busy.acquire(blocking=False):
                    continue
                try:
                    result = self.run_once()
                    log.info("plan[%s] %s", trigger, result)
                except LLMError as e:
                    log.warning("plan[%s] llm error: %s", trigger, e)
                except Exception as e:  # noqa: BLE001
                    log.exception("plan[%s] failed: %s", trigger, e)
                finally:
                    self._busy.release()
            time.sleep(1.0)
