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
    # paper trading: {feed_exchange, initial_capital, leverage, fee_rate, ...}
    paper: dict = field(default_factory=dict)

    def create_client(self):
        from .exchanges import create_exchange

        # paper 不需要交易所密钥（脱离交易所账户）
        if (self.env or "").lower() == "paper":
            return self._create_paper_client("", "")
        key, secret = load_credentials(self.env, self.api_key_env, self.api_secret_env)

        extra = {}
        ps = os.environ.get(self.api_secret_env.replace("SECRET", "PASSPHRASE") or "") or os.environ.get("EXCHANGE_PASSPHRASE")
        if ps:
            extra["passphrase"] = ps
        return create_exchange(self.exchange or "gate", env=self.env,
                               api_key=key, api_secret=secret, **extra)

    def _create_paper_client(self, key: str = "", secret: str = ""):
        """env=paper：行情委托 feed 所，交易走本地 paper_account.db。"""
        from pathlib import Path as _P
        from .exchanges import create_exchange
        from .paper.exchange import PaperExchange

        pc = dict(self.paper or {})
        feed_name = str(pc.get("feed_exchange") or self.exchange or "gate")
        feed_env = str(pc.get("feed_env") or "live")
        feed = create_exchange(feed_name, env=feed_env, api_key=key, api_secret=secret)
        store_path = pc.get("store_path")
        if not store_path:
            root = _P(os.environ.get("GATE_BOT_ROOT") or _P.cwd())
            store_path = root / "data" / "bots" / self.bot_id / "paper" / "account.db"
        cfg = {
            "initial_capital": pc.get("initial_capital", 10000),
            "leverage": pc.get("leverage", 20),
            "fee_rate": pc.get("fee_rate", 0.0005),
            "maker_fee_rate": pc.get("maker_fee_rate", 0.0),
            "funding_enabled": pc.get("funding_enabled", True),
            "position_mode": pc.get("position_mode", "single"),
            "margin_mode": pc.get("margin_mode", "isolated"),
            "price_band_pct": pc.get("price_band_pct", 5.0),
            "maintenance_margin_rate": pc.get("maintenance_margin_rate", 0.005),
            "trigger_price_type": pc.get("trigger_price_type", "latest"),
            "feed_exchange": feed_name,
        }
        return PaperExchange(env="paper", api_key=key, api_secret=secret,
                             store_path=_P(store_path), feed=feed, config=cfg)


def load_bot_config(path: Path) -> BotConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise GateApiError(f"bot config must be a mapping: {path}")
    env = str(data.get("env") or "live").lower()
    if env not in ("live", "testnet", "paper"):
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
        paper=dict(data.get("paper") or {}),
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
