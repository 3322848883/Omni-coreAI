"""Per-bot configuration loading."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .gate_client import GateApiError, GateClient, load_credentials

log = logging.getLogger("omnialpha.config")


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
    # 声明 `account: <name>` 时会与 config/accounts.yaml 的账户级风控合并
    # （上限类取更严的）—— 多 bot 共账户时的合并闸门。
    account_risk: dict = field(default_factory=dict)
    # 所属账户名（config/accounts.yaml 的 key）；空 = 不参与账户级合并
    account: str = ""
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
            root = _P(os.environ.get("OMNIALPHA_ROOT") or _P.cwd())
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


def deep_merge(base: dict, overlay: dict) -> dict:
    """深度合并：dict 递归；list/scalar 整体替换；overlay 的 null 显式删除键。"""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if v is None:
            out.pop(k, None)  # 显式删除
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def overlay_dir_for(config_dir: Path) -> Path:
    """环境覆盖目录：<config>/bots.local（与 bots/ 同级）。"""
    return Path(config_dir).parent / "bots.local"


def accounts_path(config_dir: Path) -> Path:
    return Path(config_dir).parent / "accounts.yaml"


# 上限类字段：账户级与 bot 级取**更严**（更小）的值。
# 非上限类（如 risk_pct）bot 级优先 —— bot 可以自己更保守，但不能比账户级更激进。
_RISK_CAP_KEYS = (
    "max_notional_pct", "max_total_notional_pct",
    "max_total_notional_usd", "max_notional_usd",
    "daily_loss_limit_usd", "max_leverage", "safe_mode_after_failures",
)


def load_accounts(config_dir: Path) -> dict[str, dict]:
    """账户级配置（可选）。用于多 bot 共账户时的**合并风控闸门**。

    背景（2026-10-05 架构盘点 S2/S6）：`account_risk` 逐 bot 配置、日初权益也
    按 bot 落盘 —— 多 bot 共账户时各卡各自阈值，**合计敞口 ≈ N × 阈值**，
    没有账户级合并闸门。

    文件格式（`config/accounts.yaml`，**可选；不存在则全部走 bot 级、行为不变**）：

        accounts:
          gate-main:
            api_key_env: GATE_API_KEY
            account_risk: {max_total_notional_pct: 10.0, daily_loss_limit_usd: 20}
            bots: [smc-eth-live, orderflow-eth-live]
    """
    p = accounts_path(config_dir)
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for name, cfg in (data.get("accounts") or {}).items():
        if isinstance(cfg, dict):
            out[str(name)] = dict(cfg)
    return out


def merge_account_risk(bot_risk: dict, acct_risk: dict) -> dict:
    """bot 级与账户级风控合并。

    规则：**上限类取更严的**（min）、`halt` 取逻辑或、其余 bot 级优先。
    这样账户级是硬底，而 bot 自己可以更保守 —— 但**不能比账户级更激进**。
    """
    out = dict(acct_risk or {})
    for k, v in (bot_risk or {}).items():
        if k == "halt":
            out[k] = bool(out.get(k)) or bool(v)
            continue
        if k in _RISK_CAP_KEYS and k in out:
            try:
                out[k] = min(float(out[k]), float(v))
                continue
            except (TypeError, ValueError):
                pass
        out[k] = v
    return out


def load_bot_config(path: Path, overlay_dir: Optional[Path] = None,
                    accounts: Optional[dict] = None) -> BotConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise GateApiError(f"bot config must be a mapping: {path}")
    # overlay：显式传入 > 自动推导（<config>/bots.local）
    ov_dir = Path(overlay_dir) if overlay_dir else overlay_dir_for(path.parent)
    ov = ov_dir / path.name
    ov_data: dict = {}
    if ov.is_file():
        loaded = yaml.safe_load(ov.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            ov_data = loaded
    # ── 结构性防护：enabled 只认 overlay ──
    # 基线里的 enabled 一律忽略（防误改基线把 bot 带上生产）。
    # 想启用 → 在 config/bots.local/<同名>.yaml 写 enabled: true
    baseline_enabled = bool(data.get("enabled", False))
    if baseline_enabled:
        log.warning(
            "baseline %s has enabled: true — IGNORED (enable via %s/%s instead)",
            path.name, ov_dir.name, path.name,
        )
    data = deep_merge(data, ov_data)
    enabled = bool(ov_data.get("enabled", False))
    env = str(data.get("env") or "live").lower()
    if env not in ("live", "testnet", "paper"):
        raise GateApiError(f"env must be live|testnet in {path}")
    bot_id = data.get("bot_id") or path.stem
    # 账户级风控合并：bot 声明 `account: <name>` 时，把账户级的 account_risk
    # 合进来（上限类取更严的）。未声明 / 无 accounts.yaml → 保持原行为。
    acct_name = str(data.get("account") or "").strip()
    bot_risk = dict(data.get("account_risk") or {})
    acct_risk: dict = {}
    if acct_name and accounts:
        acct_risk = dict((accounts.get(acct_name) or {}).get("account_risk") or {})
    merged_risk = merge_account_risk(bot_risk, acct_risk) if acct_risk else bot_risk
    return BotConfig(
        bot_id=str(bot_id),
        enabled=enabled,
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
        account=acct_name,
        account_risk=merged_risk,
        strategist=dict(data.get("strategist") or {}),
        paper=dict(data.get("paper") or {}),
    )


def load_all_bots(config_dir: Path, overlay_dir: Optional[Path] = None) -> dict[str, BotConfig]:
    bots: dict[str, BotConfig] = {}
    config_dir = Path(config_dir)
    if not config_dir.exists():
        return bots
    ov_dir = Path(overlay_dir) if overlay_dir else overlay_dir_for(config_dir)
    accounts = load_accounts(config_dir)
    for path in sorted(config_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        cfg = load_bot_config(path, overlay_dir=ov_dir, accounts=accounts)
        bots[cfg.bot_id] = cfg
    return bots
