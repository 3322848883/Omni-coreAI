"""Per-bot configuration loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .gate_client import GateApiError, GateClient, load_credentials


@dataclass
class BotConfig:
    bot_id: str
    enabled: bool = True
    env: str = "live"
    api_key_env: str = ""
    api_secret_env: str = ""
    symbols: list[str] = field(default_factory=list)
    max_notional_usd: Optional[float] = None
    max_orders_per_file: int = 20
    max_files_per_run: int = 50
    poll_interval_sec: float = 2.0
    label_prefix: str = ""
    # free      = no entry gating (multi-strategy / same account)
    # manage_only = with position: only add/reduce/close/tp-sl replace (no new entry)
    # strict    = same as manage_only; opposite add_* also rejected as new plan
    position_policy: str = "strict"
    # none | symbol | all — default replace before new plan (anti pile-up)
    default_replace: str = "none"
    # own = only manage orders with text prefix t-{label}; all = wipe symbol (legacy)
    order_scope: str = "own"
    # pre-trade: open/stop_entry must carry sl
    require_sl: bool = True
    # account-level risk: {halt, max_total_notional_usd, daily_loss_limit_usd, max_leverage}
    account_risk: dict = field(default_factory=dict)
    # LLM strategist (per-bot strategy + risk)
    strategist: dict = field(default_factory=dict)
    # exchange adapter: gate | binance | okx | bybit | bitget | hyperliquid
    exchange: str = "gate"

    def create_client(self):
        key, secret = load_credentials(self.env, self.api_key_env, self.api_secret_env)
        from .exchanges import create_exchange

        extra = {}
        ps = os.environ.get(self.api_secret_env.replace("SECRET", "PASSPHRASE") or "") or os.environ.get("EXCHANGE_PASSPHRASE")
        if ps:
            extra["passphrase"] = ps
        return create_exchange(self.exchange or "gate", env=self.env,
                               api_key=key, api_secret=secret, **extra)


def load_bot_config(path: Path) -> BotConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise GateApiError(f"bot config must be a mapping: {path}")
    env = str(data.get("env") or "live").lower()
    if env not in ("live", "testnet"):
        raise GateApiError(f"env must be live|testnet in {path}")
    bot_id = data.get("bot_id") or path.stem
    return BotConfig(
        bot_id=str(bot_id),
        enabled=bool(data.get("enabled", True)),
        env=env,
        api_key_env=str(data.get("api_key_env") or ""),
        api_secret_env=str(data.get("api_secret_env") or ""),
        symbols=[str(s) for s in (data.get("symbols") or [])],
        max_notional_usd=float(data["max_notional_usd"]) if data.get("max_notional_usd") is not None else None,
        max_orders_per_file=int(data.get("max_orders_per_file") or 20),
        max_files_per_run=int(data.get("max_files_per_run") or 50),
        poll_interval_sec=float(data.get("poll_interval_sec") or 2.0),
        label_prefix=str(data.get("label_prefix") or ""),
        exchange=str(data.get("exchange") or "gate").strip().lower(),
        position_policy=str(data.get("position_policy") or "strict").strip().lower(),
        default_replace=str(data.get("default_replace") or "none").strip().lower(),
        order_scope=str(data.get("order_scope") or "own").strip().lower(),
        require_sl=bool(data.get("require_sl", True)),
        account_risk=dict(data.get("account_risk") or {}),
        strategist=dict(data.get("strategist") or {}),
    )


def load_all_bots(config_dir: Path) -> dict[str, BotConfig]:
    bots: dict[str, BotConfig] = {}
    if not config_dir.exists():
        return bots
    for path in sorted(config_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        cfg = load_bot_config(path)
        bots[cfg.bot_id] = cfg
    return bots
