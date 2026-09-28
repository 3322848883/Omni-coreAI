"""AI-managed runtime event triggers (wake Plan, never place orders).

Storage: history/<bot_id>/ai_triggers.json
Semantics: same tier as interval_sec — hit → run_once (AI re-analyzes).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_ALLOW = (
    "price_break",
    "price_vs_ema",
    "ema_cross",
    "macd_cross",
    "atr_spike",
    "rsi",
    "volume_spike",
    "ma_cross",
    "boll_break",
)

DEFAULT_LIMITS = {
    "lookback": (5, 300),
    "period": (2, 200),
    "fast": (2, 200),
    "slow": (2, 200),
    "signal": (2, 50),
    "level": (1.0, 99.0),
    "mult": (1.0, 5.0),
}


class TriggerPolicyError(ValueError):
    pass


@dataclass
class AITriggerPolicy:
    enabled: bool = False
    allow_types: tuple = DEFAULT_ALLOW
    max_active: int = 5
    default_cooldown_sec: float = 60.0
    default_ttl_sec: float = 86400.0
    allow_modify: bool = True
    allow_symbols: tuple = ()  # empty = use bot symbols
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))


@dataclass
class AITrigger:
    id: str
    type: str
    symbol: str
    params: dict
    cooldown_sec: float = 60.0
    ttl_sec: float = 86400.0
    created_at: float = 0.0
    expire_at: float = 0.0
    last_fire: float = 0.0
    reason: str = ""
    source: str = "llm"

    def to_condition(self) -> dict:
        d = dict(self.params)
        d.update(
            {
                "type": self.type,
                "symbol": self.symbol,
                "cooldown_sec": self.cooldown_sec,
                "key": self.id,
            }
        )
        return d


def validate_trigger_payload(
    raw: dict,
    policy: AITriggerPolicy,
    bot_symbols: Optional[list] = None,
) -> dict:
    """Return normalized params; raise TriggerPolicyError if not allowed."""
    if not isinstance(raw, dict):
        raise TriggerPolicyError("trigger must be object")
    ctype = str(raw.get("type") or "").strip().lower()
    if ctype not in tuple(policy.allow_types):
        raise TriggerPolicyError(f"type not allowed: {ctype!r}")
    symbol = str(raw.get("symbol") or "").strip().upper()
    allowed = tuple(policy.allow_symbols) or tuple(bot_symbols or [])
    if allowed and symbol not in allowed:
        raise TriggerPolicyError(f"symbol not allowed: {symbol!r}")

    params: dict[str, Any] = {}
    for key in ("period", "fast", "slow", "signal", "lookback", "mult", "level",
                "side", "dir", "op", "k", "ma", "candles"):
        if key in raw and raw[key] is not None:
            params[key] = raw[key]
    # numeric limits: policy.limits overrides DEFAULT_LIMITS
    limits = dict(DEFAULT_LIMITS)
    limits.update(dict(policy.limits or {}))
    for key, (lo, hi) in limits.items():
        if key in params and isinstance(params[key], (int, float)):
            v = float(params[key])
            if v < lo or v > hi:
                raise TriggerPolicyError(f"{key}={v} out of [{lo},{hi}]")
    cooldown = float(raw.get("cooldown_sec") or policy.default_cooldown_sec)
    ttl = float(raw.get("ttl_sec") or policy.default_ttl_sec)
    if cooldown < 1 or cooldown > 86400:
        raise TriggerPolicyError("cooldown_sec out of range")
    return {
        "type": ctype,
        "symbol": symbol,
        "params": params,
        "cooldown_sec": cooldown,
        "ttl_sec": ttl,
        "reason": str(raw.get("reason") or "")[:200],
    }


class AITriggerStore:
    def __init__(self, path: Path, policy: Optional[AITriggerPolicy] = None):
        self.path = Path(path)
        self.policy = policy or AITriggerPolicy()
        self._items: list[AITrigger] = []
        self.load()

    def load(self) -> None:
        self._items = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        for row in data.get("items") or []:
            try:
                self._items.append(
                    AITrigger(
                        id=str(row.get("id")),
                        type=str(row.get("type")),
                        symbol=str(row.get("symbol")),
                        params=dict(row.get("params") or {}),
                        cooldown_sec=float(row.get("cooldown_sec") or 60),
                        ttl_sec=float(row.get("ttl_sec") or 86400),
                        created_at=float(row.get("created_at") or 0),
                        expire_at=float(row.get("expire_at") or 0),
                        last_fire=float(row.get("last_fire") or 0),
                        reason=str(row.get("reason") or ""),
                        source=str(row.get("source") or "llm"),
                    )
                )
            except Exception:  # noqa: BLE001
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "items": [asdict(t) for t in self._items],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _purge_expired(self, now: Optional[float] = None) -> None:
        ts = now if now is not None else time.time()
        self._items = [t for t in self._items if t.expire_at > ts]

    def active(self, now: Optional[float] = None) -> list[AITrigger]:
        self._purge_expired(now)
        return list(self._items)

    def add(self, norm: dict, bot_symbols: Optional[list] = None) -> AITrigger:
        if not self.policy.enabled:
            raise TriggerPolicyError("ai_triggers.enabled=false")
        self._purge_expired()
        if len(self._items) >= int(self.policy.max_active):
            raise TriggerPolicyError(f"trigger_limit: max_active={self.policy.max_active}")
        now = time.time()
        t = AITrigger(
            id="t-" + uuid.uuid4().hex[:8],
            type=norm["type"],
            symbol=norm["symbol"],
            params=norm.get("params") or {},
            cooldown_sec=float(norm.get("cooldown_sec") or self.policy.default_cooldown_sec),
            ttl_sec=float(norm.get("ttl_sec") or self.policy.default_ttl_sec),
            created_at=now,
            expire_at=now + float(norm.get("ttl_sec") or self.policy.default_ttl_sec),
            reason=norm.get("reason") or "",
        )
        self._items.append(t)
        self.save()
        return t

    def remove(self, trigger_id: str) -> bool:
        if not self.policy.allow_modify:
            raise TriggerPolicyError("allow_modify=false")
        before = len(self._items)
        self._items = [t for t in self._items if t.id != trigger_id]
        changed = len(self._items) != before
        if changed:
            self.save()
        return changed

    def replace_all(self, norms: list[dict], bot_symbols: Optional[list] = None) -> list[AITrigger]:
        if not self.policy.allow_modify:
            raise TriggerPolicyError("allow_modify=false")
        self._items = []
        out = []
        for n in norms:
            out.append(self.add(n, bot_symbols=bot_symbols))
        return out

    def mark_fired(self, trigger_id: str, ts: Optional[float] = None) -> None:
        for t in self._items:
            if t.id == trigger_id:
                t.last_fire = ts if ts is not None else time.time()
        self.save()
