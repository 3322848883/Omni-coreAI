# -*- coding: utf-8 -*-
"""Bot 配置的**键白名单** —— 未知键不再静默忽略。

背景（2026-10-14 全面检查 N1）：`account_risk` / `strategist` / `paper` 等段此前
都是**自由 dict**，任何 `.get("...")` 读不到的键**静默 no-op**。实测先例：5 个
`ab-*` bot 配了 `account_risk.auto_protect: "dry"` 却没配 `auto_protect_sl_pct`，
dry 观察期其实什么都没观察到（`ensure_protection` 在 `pct<=0` 时早退）——
**「配了但不生效」是本仓最贵的失败形态**，而键名写错（typo）连告警都没有。

本模块把「哪些键是合法的」变成一份**可校验、可生成文档、可测漂移**的白名单。

来源（三路并集，见 `scripts/gen_config_reference.py --audit-sources`）：
  ① `config/bots/*.yaml` + `config/bots.local/*.yaml` 里**实际出现**的键
  ② 各 dataclass 字段（`BotConfig` / `StrategistConfig` / `MarketConfig` / `RiskConfig`）
  ③ 代码里**定向核实过**的消费键（`providers._build_config` / `_create_paper_client` …）

**阶段 1 = 只告警不报错**（本文件即阶段 1 的依据）。理由同 `_fail_or_warn` 的
既定原则：存量配置里若真有一批「写了但没人读」的键，一律报错等于拦住所有人，
会逼人绕过校验 —— 那比不校验更糟。等存量清干净再考虑对**启用中**的 bot 报错。

设计约束：**不引新依赖**（纯 `set`/`frozenset`，不用 pydantic）。
"""
from __future__ import annotations

from typing import Any

# ── ① Bot yaml 顶层键 ────────────────────────────────────────
TOP_LEVEL: frozenset[str] = frozenset({
    # BotConfig 字段
    "account", "account_risk", "account_scope", "allow_close", "allow_reduce",
    "api_key_env", "api_secret_env", "bot_id", "default_replace", "enabled",
    "env", "exchange", "label_prefix", "manage_positions", "max_files_per_run",
    "max_notional_usd", "max_orders_per_file", "max_scan_symbols", "order_scope",
    "paper", "place_before_cancel", "poll_interval_sec", "position_policy",
    "require_sl", "root", "strategist", "symbols", "symbols_unrestricted",
    # 加载期开关 / 账户级
    "validate_contracts", "accounts", "groups",
})

# ── ② account_risk 段 ────────────────────────────────────────
# 注意：必须覆盖 `config._RISK_CAP_KEYS`（账户级合并的「越小越严」字段）——
# 有测试 `test_risk_cap_keys_are_whitelisted` 守着，新增合并键忘了加白名单会红。
ACCOUNT_RISK: frozenset[str] = frozenset({
    "auto_protect", "auto_protect_sl_pct", "grid_tp_auto", "daily_loss_limit_usd",
    "equity_deviation_halt", "equity_deviation_halt_pct", "give_back", "halt",
    "max_leverage", "max_notional_pct", "max_notional_usd",
    "max_total_notional_pct", "max_total_notional_usd", "no_flip",
    # 保证金模式（cross|isolated）。**live 侧唯一的配置入口** —— 此前只有
    # `paper.margin_mode`（纸面引擎）有配置，live 只能靠 AI chip 的
    # `intent.margin_mode`，而人格字段表/契约都没有该字段 → 从未生效。
    # Gate 只允许「无持仓无挂单」时切换，故 executor 侧是幂等 + 失败静默。
    "margin_mode",
    "peak_trail", "peak_trail_atr",
    "risk_pct", "risk_per_trade_pct", "safe_mode_after_failures",
    "total_risk_pct",
})

# ── ③ strategist 段 ──────────────────────────────────────────
STRATEGIST: frozenset[str] = frozenset({
    "account_risk", "ai_triggers", "bot_id", "bot_root", "candles",
    "check_interval_sec", "cond_gap_sec", "conditions", "enabled", "env",
    "event_on_kline_close", "event_timeframe", "interval_sec", "llm",
    "manage_positions", "market", "prompt_file", "risk", "skills",
    "skills_required", "symbols", "timeframe", "tools", "vision",
    "vision_timeframes", "write_hold",
})

# ── ④ strategist.llm 段（providers._build_config 的 pick() 全集）──
STRATEGIST_LLM: frozenset[str] = frozenset({
    "api_key_env", "base_url", "base_url_env", "default_base_url", "fallback",
    "json_mode", "max_tokens", "model", "provider", "reasoning_effort",
    "temperature", "thinking", "timeout_sec", "user_id", "user_id_prefix",
})

# ── ⑤ strategist.market 段（MarketConfig）────────────────────
STRATEGIST_MARKET: frozenset[str] = frozenset({
    "db", "exchange", "extra_candles", "extra_timeframes", "health_url",
    "indicators", "mode", "pa_data_root", "refresh", "stale_factor",
    "timeframes",
})

# ── ⑥ strategist.risk 段（RiskConfig + 账户级合并键）─────────
STRATEGIST_RISK: frozenset[str] = frozenset({
    "allow_actions", "drop_on_low_conf", "max_chips", "max_chips_per_symbol",
    "max_notional_pct", "max_notional_usd", "min_confidence", "per_symbol",
    "risk_pct", "risk_per_trade_pct", "symbols", "total_risk_pct",
})

# ── ⑦ strategist.tools 段（含 F4 新增的预算/超时键）──────────
STRATEGIST_TOOLS: frozenset[str] = frozenset({
    "allow", "deny", "enabled", "max_rounds", "native",
    # F4：工具预算与超时（默认 null = 不限，行为与现状一致）
    "max_tokens", "max_calls", "timeout_sec",
})

# ── ⑧ strategist.ai_triggers 段 ──────────────────────────────
STRATEGIST_AI_TRIGGERS: frozenset[str] = frozenset({
    "allow_modify", "allow_types", "default_cooldown_sec", "default_ttl_sec",
    "enabled", "max_active",
})

# ── ⑨ paper 段 ───────────────────────────────────────────────
PAPER: frozenset[str] = frozenset({
    "fee_rate", "feed_env", "feed_exchange", "funding_enabled",
    "initial_capital", "leverage", "maintenance_margin_rate", "maker_fee_rate",
    "margin_mode", "position_mode", "price_band_pct", "store_path",
    "tick_interval_sec", "trigger_price_type",
})

# ── ⑩ accounts.yaml 的账户条目 ───────────────────────────────
ACCOUNT_ENTRY: frozenset[str] = frozenset({
    "account_risk", "api_key_env", "bots",
})

#: 路径 → 该层允许的键。键是「点分路径」（相对 bot yaml 根）。
SCHEMA: dict[str, frozenset[str]] = {
    "": TOP_LEVEL,
    "account_risk": ACCOUNT_RISK,
    "strategist": STRATEGIST,
    "strategist.llm": STRATEGIST_LLM,
    "strategist.market": STRATEGIST_MARKET,
    "strategist.risk": STRATEGIST_RISK,
    "strategist.tools": STRATEGIST_TOOLS,
    "strategist.ai_triggers": STRATEGIST_AI_TRIGGERS,
    "paper": PAPER,
}

#: 供文档生成：路径 → 一句话说明
DESCRIPTIONS: dict[str, str] = {
    "": "Bot 配置文件顶层（`config/bots/<id>.yaml`；启用走 `config/bots.local/`）",
    "account_risk": "风控闸门（可与 `config/accounts.yaml` 的账户级值合并，上限取更严）",
    "strategist": "策略引擎段（不写 = 该 bot 只跑外部信号）",
    "strategist.llm": "LLM 供应商与采样参数（覆盖 `config/providers.yaml` 的默认值）",
    "strategist.market": "行情数据源（rest / hybrid / local，K 线周期与指标）",
    "strategist.risk": "计划层风控（min_confidence / max_chips / allow_actions …）",
    "strategist.tools": "原生工具开关与预算（enabled / native / allow / deny …）",
    "strategist.ai_triggers": "AI 自设触发器策略（上限、冷却、TTL）",
    "paper": "模拟盘参数（env: paper 时生效）",
}


def find_unknown_keys(data: dict[str, Any]) -> list[str]:
    """返回 `data` 里**不在白名单**的键（点分路径，已排序）。

    只下钻白名单里声明过的**嵌套路径**；未声明为嵌套的 dict 值（如
    `strategist.llm.extra_headers` 这类自由映射）不做检查 —— 宁漏报不误报。
    """
    unknown: list[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        allowed = SCHEMA.get(path)
        if allowed is None:            # 该层未纳入白名单 → 不检查（宁漏报）
            return
        for key in node:
            if key not in allowed:
                unknown.append(f"{path}.{key}" if path else str(key))
        for key, val in node.items():
            child = f"{path}.{key}" if path else str(key)
            if child in SCHEMA and isinstance(val, dict):
                walk(val, child)

    walk(data, "")
    return sorted(unknown)
