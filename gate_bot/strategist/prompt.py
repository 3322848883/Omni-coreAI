"""Prompt assembly (VergeX-style multi-chip decision)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema import CHIP_ACTIONS, CHIP_ORDER_TYPES

_SYSTEM_HEAD = (
    "你是谨慎的加密货币永续策略引擎（VergeX 风格多品种决策）。\\n"
    "目标：保本优先，宁可 hold，不可乱开仓。\\n"
    "只输出一个 JSON 对象，不要 Markdown 前后缀。\\n"
    '格式: {"cycle_id":"...","reasoning":"...","chips":[{"symbol":"BTC_USDT",'
    '"action":"open_long|open_short|add_long|add_short|reduce_long|reduce_short|close|close_all|hold|'
    'stop_entry_long|stop_entry_short|flatten|cancel_all|cancel_price_all",'
    '"confidence":0.0,"size_usd":50,"tp":null,"sl":null,"type":"market|limit|post_only|ioc|fok",'
    '"price":null,"trigger_price":null,"reasoning":"..."}]}\\n'
    "规则: 1) action 英文枚举; 突破进场用 stop_entry_*，止损止盈用 tp/sl。\\n"
    "2) confidence 0~1，低于 min_confidence 应 hold。\\n"
    "3) open/add/stop_entry 必须 size_usd 或 size。\\n"
    "4) type=limit 等必须给 price。\\n"
    "5) 同 symbol 优先管理已有仓。\\n"
    "6) 不确定就 hold。\\n"
)

PLAN_SCHEMA_HINT = (
    "allowed_actions="
    + ",".join(sorted(CHIP_ACTIONS))
    + " order_types="
    + ",".join(sorted(CHIP_ORDER_TYPES))
)

SYSTEM_PROMPT = _SYSTEM_HEAD + PLAN_SCHEMA_HINT


def load_strategy_prompt(path: str | Path | None) -> str:
    if not path:
        return (
            "策略：趋势跟随。跟随明显方向，震荡 hold。"
            "有仓时优先管理；新仓必须带 sl。单币单计划。"
        )
    return Path(path).read_text(encoding="utf-8")


def build_system_prompt(strategy_prompt: str) -> str:
    return SYSTEM_PROMPT + "\n\n【策略人格】\n" + strategy_prompt


def build_user_prompt(snapshot: dict[str, Any], risk: dict[str, Any], symbols: list[str]) -> str:
    return (
        "【品种宇宙】\n"
        + json.dumps(symbols, ensure_ascii=False)
        + "\n\n【策略风控】\n"
        + json.dumps(risk, ensure_ascii=False)
        + "\n\n【市场与账户快照】\n"
        + json.dumps(snapshot, ensure_ascii=False)
        + "\n\n请输出本轮 Plan JSON。"
    )


def build_messages(strategy_prompt: str, snapshot: dict[str, Any], risk: dict[str, Any], symbols: list[str]):
    return build_system_prompt(strategy_prompt), build_user_prompt(snapshot, risk, symbols)
