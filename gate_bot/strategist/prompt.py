"""Prompt assembly: fixed system contract + strategy persona (role/style)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .schema import CHIP_ACTIONS, CHIP_ORDER_TYPES

# Fixed contract only: output format + hard rules. Role/style live in the strategy persona.
_SYSTEM_HEAD = (
    "只输出一个 JSON 对象，不要 Markdown 前后缀。\n"
    '格式: {"cycle_id":"...","reasoning":"...","chips":[{"symbol":"BTC_USDT",'
    '"action":"open_long|open_short|add_long|add_short|reduce_long|reduce_short|close|close_all|hold|'
    'stop_entry_long|stop_entry_short|flatten|cancel_all|cancel_price_all|modify_tp_sl",'
    '"confidence":0.0,"size_usd":50,"tp":null,"tp2":null,"tp1_share":null,"sl":null,"type":"market|limit|post_only|ioc|fok",'
    '"price":null,"trigger_price":null,"leverage":null,"side":"long|short|null",'
    '"tp_mode":"trigger|limit_order","sl_mode":"trigger|limit_order","reasoning":"..."}],'
    '"triggers":[]}\n'
    "规则: 1) action 英文枚举; 突破进场用 stop_entry_*+trigger_price，止损止盈用 tp/sl。\n"
    "2) confidence 0~1，低于 min_confidence 应 hold。\n"
    "3) 仓位优先写 size_usd（名义 USDT）；size 是合约张数。用 size 前必须查该 symbol 的 "
    "contract.min_notional_usd / quanto_multiplier（1张≈min_notional_usd 名义，不足1张会被拒）。\n"
    "4) type=limit 等必须给 price。\n"
    "5) 同 symbol 优先管理已有仓。\n"
    "6) 不确定就 hold。\n"
    "7) reasoning 必须 ≤30 字，禁止长篇分析。\n"
    "8) 移动/修改持仓的止盈止损：action=modify_tp_sl 并给 tp/sl（可只改一边）；"
    "或 action=hold 且带 tp/sl。只写 hold 不带 tp/sl = 不改任何保护单。\n"
    "9) 双止盈/分批/网格若写进计划，JSON 必须落实字段（swing 开仓给 tp+tp2+tp1_share；"
    "缺字段=未完成，禁止只在 reasoning 里写 TP1/TP2）。\n"
    "10) side=long|short 用于双仓持仓管理（close/modify_tp_sl）；单仓可省略。\n"
    "11) tp_mode/sl_mode 默认 trigger（条件计划委托）；limit_order=盘口 reduce_only 限价。\n"
    "12) triggers[] 可选：自设唤醒条件（如 {type:price_break,symbol,lookback,side:high|low}），"
    "命中后重新分析，不直接下单。\n"
)

PLAN_SCHEMA_HINT = (
    "allowed_actions="
    + ",".join(sorted(CHIP_ACTIONS))
    + " order_types="
    + ",".join(sorted(CHIP_ORDER_TYPES))
)

SYSTEM_PROMPT = _SYSTEM_HEAD + PLAN_SCHEMA_HINT


def build_system_prompt(strategy_prompt: str, tools_guide: str = "") -> str:
    """Fixed contract + optional tool guide (system layer) + strategy persona."""
    head = SYSTEM_PROMPT
    if tools_guide:
        head = head + "\n\n" + tools_guide
    return head + "\n\n【策略人格】\n" + strategy_prompt


def load_strategy_prompt(
    path: str | Path | None,
    prompts_root: str | Path | None = None,
    bot_root: str | Path | None = None,
) -> str:
    """Load strategy persona text (role + style). Restricted to prompts/ (no arbitrary FS read).

    Resolution order is cwd-independent when `bot_root` (or `prompts_root`) is given:
    absolute path under prompts/, bot_root/<path>, prompts_root/<path>, prompts_root/<name>.
    """
    if not path:
        return (
            "你是加密货币永续合约策略引擎。"
            "核心原则：保住本金，其次才是收益。"
            "策略：趋势跟随。跟随明显方向，震荡 hold。"
            "有仓时优先管理；新仓必须带 sl。单币单计划。"
        )
    if prompts_root is not None:
        root = Path(prompts_root).expanduser().resolve()
    elif bot_root is not None:
        root = (Path(bot_root).expanduser().resolve() / "prompts")
    else:
        root = (Path.cwd() / "prompts").resolve()
    raw = Path(path)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw.resolve())
    else:
        # Prefer project root over cwd so systemd/cron --root still works
        bases = []
        if bot_root is not None:
            bases.append(Path(bot_root).expanduser().resolve())
        bases.append(Path.cwd())
        for base in bases:
            candidates.append((base / raw).resolve())
        candidates.append((root / raw).resolve())
        candidates.append((root / raw.name).resolve())

    def _under_root(p: Path) -> bool:
        try:
            return p == root or root in p.parents
        except Exception:  # noqa: BLE001
            return False

    for cand in candidates:
        if _under_root(cand) and cand.is_file():
            return cand.read_text(encoding="utf-8")
    raise PermissionError(f"prompt_file must exist under {root}: {path}")


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


def build_messages(
    strategy_prompt: str,
    snapshot: dict[str, Any],
    risk: dict[str, Any],
    symbols: list[str],
    chart_base64: Optional[str] = None,
):
    """构建消息列表。chart_base64 非空时附 K 线图（vision 模式）。"""
    system = build_system_prompt(strategy_prompt)
    text = build_user_prompt(snapshot, risk, symbols)

    if chart_base64:
        # OpenAI/DeepSeek vision 格式
        user_content: Any = [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {"url": chart_base64},
            },
        ]
    else:
        user_content = text

    return system, user_content
