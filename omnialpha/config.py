"""Per-bot configuration loading."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .gate_client import GateApiError, GateClient, load_credentials, resolve_symbol

log = logging.getLogger("omnialpha.config")


@dataclass
class BotConfig:
    bot_id: str
    enabled: bool = True
    env: str = "live"
    api_key_env: str = ""
    api_secret_env: str = ""
    symbols: list[str] = field(default_factory=list)
    # 显式声明「本 bot 不限制品种」。`symbols: []` 必须配它，否则启动报错 ——
    # 空白名单会被执行器当成 None（任意币可开仓 + 零守护），是静默放开而非「没配」（C-1）。
    symbols_unrestricted: bool = False
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
    # 熔断粒度：auto（有 account: 就按账户，否则按 bot）| bot | <账户名>。
    # 只解析不消费 —— 消费方是熔断（T19）：逐 bot 熔断时同账户其他 bot 会继续开仓。
    account_scope: str = "auto"
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
    # 熔断阈值也是「越小越严」：账户级配 5% 时，bot 配 10% 应被收紧到 5%
    "equity_deviation_halt_pct",
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


# ── symbol 归一 + 配置 fail-fast（T4）───────────────────────────────
# 为什么全部落在配置加载**一处**：配置是币种的唯一权威来源（设计文档 S2.1 第③层）。
# 放到执行器/快照里去校验，就又是「配置看起来生效、实际不生效」——本仓反复踩的形态。

def normalize_symbols(seq) -> list[str]:
    """逐项归一（`btcusdt`→`BTC_USDT`）→ 去重 → **保序**。

    保序不是洁癖：`symbols[0]` 在若干处被当「首币」（降级兜底、K 线收盘唤醒），
    顺序一变行为就变，而单币 bot 的输出必须逐字不变（I11）。
    """
    out: list[str] = []
    for raw in (seq or []):
        text = str(raw or "").strip()
        if not text:
            raise GateApiError(f"symbols 里有空项: {seq!r}")
        sym = resolve_symbol(text)
        if sym not in out:
            out.append(sym)
    return out


def _fail_or_warn(path: Path, msg: str, *, enabled: bool) -> None:
    """启用中的 bot 直接报错；未启用的只告警。

    为什么区分：基线 `config/bots/*.yaml` 里有大量别人的实验配置（本仓 60 份），
    新校验若对它们一律报错，等于**拦住所有人**——那会逼人绕过校验，比不校验更糟。
    """
    if enabled:
        raise GateApiError(f"{path.name}: {msg}")
    log.warning("%s: %s（未启用，仅告警）", path.name, msg)


def _warn_persona_symbols(bot_path: Path, prompt_file, symbols: list,
                          unrestricted: bool) -> None:
    """人格文件里写了**具体合约代码**、而它不在本 bot 白名单里 → 告警。

    为什么只告警不报错：人格里出现 `BTC_USDT` 也可能是**叙事引用**
    （"禁止把 BTC 的结构讲成 ETH 的结构"），那是要保留的写法 —— 静态文本分不清
    「引用」与「要求的标的」，所以留痕让人判断。真矛盾时模型会拿不到那个币的数据。
    """
    if not prompt_file or unrestricted or not symbols:
        return
    p = Path(str(prompt_file))
    if not p.is_absolute():
        # config/bots/<bot>.yaml → 仓库根
        p = bot_path.parent.parent.parent / p
    if not p.is_file():
        return
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    mentioned = sorted({m.upper() for m in re.findall(r"\b[A-Z]{2,6}_USDT\b", text)})
    outside = [m for m in mentioned if m not in symbols]
    if outside:
        log.warning("%s: 人格 %s 提到 %s，不在 symbols %s 内"
                    "（叙事引用可忽略；若是要求的标的，那些币的数据与下单都会被拒）",
                    bot_path.name, p.name, outside, symbols)


def _contract_check_enabled(data: dict) -> bool:
    """合约存在性校验是**可选/懒**校验：yaml `validate_contracts: true` 或
    `OMNIALPHA_VALIDATE_CONTRACTS=1` 才做，默认关。

    为什么默认关：配置加载会发生在离线测试、只读巡检、容器构建里。把「网络可达」
    变成启动前提，会把这些场景一起拦下（测试还会真的发请求）——代价远大于收益。
    """
    if data.get("validate_contracts") is True:
        return True
    return os.environ.get("OMNIALPHA_VALIDATE_CONTRACTS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


_CONTRACTS_CACHE: dict[tuple[str, str], Optional[set]] = {}


def fetch_exchange_contracts(exchange: str, env: str = "live") -> Optional[set]:
    """该所当前可用合约名集合；**取不到就返回 None**（调用方跳过校验，绝不拦启动）。

    只实现 Gate（公开端点 `/futures/usdt/contracts`，无需密钥）：其余五所的适配器
    要先有密钥才能建 client，把「配置校验」变成「凭据校验」是更坏的取舍（宁可少校验
    一所，也不能让无密钥/离线环境起不来）。T16 补各所真实元数据时再铺开。
    """
    key = ((exchange or "gate").strip().lower(), env)
    if key in _CONTRACTS_CACHE:
        return _CONTRACTS_CACHE[key]
    result: Optional[set] = None
    if key[0] == "gate":
        try:
            client = GateClient(api_key="", api_secret="",
                                env=env if env in ("live", "testnet") else "live", timeout=10)
            result = set(client.get_contracts().keys())
        except Exception as e:  # noqa: BLE001 — 校验是尽力而为，网络失败不算配置错误
            log.warning("合约存在性校验: 拉取 %s 合约列表失败，本次跳过（%s）", key[0], e)
            result = None
    else:
        log.info("合约存在性校验: exchange=%s 暂未实现（跳过）", key[0])
    _CONTRACTS_CACHE[key] = result
    return result


# 测试可替换（离线环境不得真的发请求）
_CONTRACT_FETCHER = fetch_exchange_contracts


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

    # ── 币种：归一 / 去重 / 白名单自洽 / 显式不限制 ──
    # 币种是配置层的唯一权威（设计 S2.1 第③层）。这里不归一，下游每一处都要自己猜，
    # 而"猜错"在本仓的历史形态**全是静默的**：`symbols: [btc]` 会让快照 ticker 报错、
    # 让工具宇宙比对失配（`given not in uni`），看起来像"这个币没数据"。
    raw_symbols = data.get("symbols") or []
    if not isinstance(raw_symbols, list):
        raise GateApiError(f"{path.name}: symbols 必须是列表")
    symbols = normalize_symbols(raw_symbols)
    unrestricted = bool(data.get("symbols_unrestricted", False))
    if unrestricted and symbols:
        raise GateApiError(
            f"{path.name}: symbols_unrestricted: true 与 symbols: [...] 语义矛盾 —— "
            f"放开品种限制就别再列白名单，只能给一个")
    if not symbols and not unrestricted:
        # 执行器把 `[]` 当 None = **不限制**（任意币可开仓），而守护扫描只遍历
        # `bot.symbols`（零次迭代 = 零守护）。能力保留，但必须**显式**声明，
        # 不再靠「空列表」暗示（那正是「配置看起来生效、实际不生效」）。
        _fail_or_warn(
            path,
            "symbols 为空但未声明 symbols_unrestricted: true —— 空白名单等于"
            "「任意币可开仓 + 零守护」；确实要放开请显式写 symbols_unrestricted: true",
            enabled=enabled)
    strategist = dict(data.get("strategist") or {})
    if strategist.get("symbols") is not None:
        strategist["symbols"] = normalize_symbols(strategist["symbols"])
    # 分析宇宙必须 ⊆ 执行白名单：超出部分会在执行层被拒单（白烧一轮），
    # 而不是"模型想分析就分析"。
    if symbols and strategist.get("symbols"):
        outside = [s for s in strategist["symbols"] if s not in symbols]
        if outside:
            _fail_or_warn(
                path,
                f"strategist.symbols {outside} 不在 bot.symbols {symbols} 内"
                f"（分析宇宙超出执行白名单，超出的币一律会被执行层拒单）",
                enabled=enabled)
    scope = data.get("account_scope", "auto")
    if not isinstance(scope, str) or not scope.strip():
        raise GateApiError(f"{path.name}: account_scope 必须是 auto|bot|<账户名>")
    scope = scope.strip()

    # ── 账户归属：`account_scope` 是权威（T19）────────────────────────
    # 熔断标记（`halt.json`）与日初权益都按这个归属**共享**：同一个账户上的多个 bot
    # 必须一起停 —— 否则一个 bot 亏到熔断、另一个还在拿同一笔钱开仓（实盘安全级）。
    #   `bot`      = 显式声明「本 bot 独立」，即使配了 `account` 也不拿它做共享归属
    #   `<账户名>` = 直接指定（此时熔断走 `data/accounts/<name>/state/halt.json`）
    #   `auto`     = 沿用旧的 `account` 字段（默认，行为不变）
    if scope == "bot":
        eff_account = ""
    elif scope != "auto":
        eff_account = scope
    else:
        eff_account = acct_name
    if scope not in ("auto", "bot") and accounts and scope not in accounts:
        _fail_or_warn(
            path,
            f"account_scope={scope!r} 在 config/accounts.yaml 里不存在"
            f"（现有账户：{sorted(accounts)}）—— 熔断标记会落到一个没人读的路径",
            enabled=enabled)
    exchange = str(data.get("exchange") or "gate").strip().lower()
    # 合约存在性：**可选/懒**校验，默认关（理由见 _contract_check_enabled）。
    # 取不到合约列表（离线/无凭据）就跳过 —— 配置校验不得变成"网络可达"的前置。
    if _contract_check_enabled(data) and symbols:
        available = _CONTRACT_FETCHER(exchange, env)
        if available is None:
            # 分清「取不到」与「这家所压根没接校验」：前者是故障（值得重试），
            # 后者是已知状态（别让人去查一个不存在的能力）。
            from .exchanges.coverage import supports_listing

            if not supports_listing(exchange):
                log.info("%s: %s 的合约存在性校验尚未接入（目前只有 gate），跳过",
                         path.name, exchange)
            else:
                log.info("%s: 合约列表不可得，跳过存在性校验", path.name)
        else:
            missing = [s for s in symbols if s not in available]
            if missing:
                _fail_or_warn(
                    path,
                    f"exchange={exchange} 上没有这些合约: {missing}（拼错？或换 exchange）",
                    enabled=enabled)

    # 人格文件写的标的 ⊄ 白名单 → 告警（不报错，理由见该函数 docstring）
    _warn_persona_symbols(path, strategist.get("prompt_file"), symbols, unrestricted)

    return BotConfig(
        bot_id=str(bot_id),
        enabled=enabled,
        env=env,
        api_key_env=str(data.get("api_key_env") or ""),
        api_secret_env=str(data.get("api_secret_env") or ""),
        symbols=symbols,
        symbols_unrestricted=unrestricted,
        account_scope=scope,
        max_notional_usd=float(data["max_notional_usd"]) if data.get("max_notional_usd") is not None else None,
        max_orders_per_file=int(data.get("max_orders_per_file") or 20),
        max_files_per_run=int(data.get("max_files_per_run") or 50),
        poll_interval_sec=float(data.get("poll_interval_sec") or 2.0),
        label_prefix=str(data.get("label_prefix") or ""),
        exchange=exchange,
        position_policy=str(data.get("position_policy") or "strict").strip().lower(),
        default_replace=str(data.get("default_replace") or "none").strip().lower(),
        order_scope=str(data.get("order_scope") or "own").strip().lower(),
        require_sl=bool(data.get("require_sl", True)),
        account=eff_account,
        account_risk=merged_risk,
        strategist=strategist,
        paper=dict(data.get("paper") or {}),
    )


def check_label_prefixes(bots: dict[str, BotConfig]) -> None:
    """`label_prefix` 全局唯一（C-7）：它是**归属判据**（订单 text `t-<prefix>`），
    重名 = 两个 bot 的 `_text_owned` 互相命中 → 互相撤单/改单/对账。

    为什么只在**看得到全部 bot** 的这一层做：单看每个 bot 都合法，放在一起才出事；
    而且报错必须点出冲突双方，否则用户不知道改哪一个。

    两条收敛口径：
    - 空 prefix 不参与（它是「没声明命名空间」的历史默认值，多 bot 的测试目录普遍如此）；
    - 只有**两个以上在跑**的 bot 共用才报错 —— 未启用的 bot 既不在下单，也不该拦住
      别人的实验配置（仓库实况：`ofl`/`wyk`/`sc` 三对重名，每对只有一个在跑）。
    """
    running = {bid for bid, b in bots.items() if b.enabled}
    by_prefix: dict[str, list[str]] = {}
    for bid, b in bots.items():
        if b.label_prefix:
            by_prefix.setdefault(b.label_prefix, []).append(bid)
    for prefix, bids in sorted(by_prefix.items()):
        if len(bids) < 2:
            continue
        active = sorted(x for x in bids if x in running)
        msg = (f"label_prefix {prefix!r} 重复: {sorted(bids)}"
               f"（重名会让两个 bot 互相撤单/改单）")
        if len(active) >= 2:
            raise GateApiError(msg + f" —— 其中 {active} 都在启用状态，"
                                     f"请在 config/bots.local/<bot>.yaml 里错开")
        # 仓库实况：三对重名里各只有一个在跑 —— 不误报，只留痕
        log.warning("%s（只有 %s 在运行，仅告警；未启用的一侧若有存量挂单仍会被误撤）",
                    msg, active or "无")


def assert_account_risk_consistent(bot: BotConfig, config_dir: Path,
                                   overlay_dir: Optional[Path] = None) -> None:
    """断言该 bot 看到的 `account_risk` 与「经 accounts 合并后」完全一致。

    为什么要断言：`run` 路径经 `load_all_bots`（合并 `config/accounts.yaml`），
    而 `plan`/`plan-loop` 曾经直接 `load_bot_config`（**不合并**）—— 于是 strategist
    会把比执行闸门**更宽松**的预算告诉模型，模型按大预算报量然后被拒单
    （实盘曾占失败的 1/7）。两处口径必须同源，不能靠"记得传 accounts"。
    """
    path = Path(config_dir) / f"{bot.bot_id}.yaml"
    if not path.is_file():
        log.warning("account_risk 一致性断言跳过：找不到 %s（bot_id 与文件名不一致？）", path)
        return
    accounts = load_accounts(Path(config_dir))
    fresh = load_bot_config(path, overlay_dir=overlay_dir, accounts=accounts)
    if dict(fresh.account_risk or {}) != dict(bot.account_risk or {}):
        raise GateApiError(
            f"{bot.bot_id}: account_risk 与 accounts 合并后的结果不一致 —— "
            f"strategist 预算会与执行闸门漂移：{bot.account_risk} != {fresh.account_risk}")


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
    check_label_prefixes(bots)
    return bots
