"""上下文拼装器：按缓存优化顺序组装每轮 prompt。

固定前缀（缓存命中区）: system → 工具 → 订单框架 → recent_events 框架
动态后缀（缓存未命中区）: 订单值 → recent_events 内容 → 快照
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .journal import MemoryJournal
from .profile import MemoryProfile


def build_context(
    root: Path,
    bot_id: str,
    *,
    system_prompt: str,
    order_context: Optional[dict],
    snapshot: dict,
    n_recent: int = 3,
) -> dict:
    """组装每轮主上下文。

    返回 {"messages": [...], "cacheable_prefix_tokens": int, "dynamic_tokens": int}
    """
    journal = MemoryJournal(root, bot_id)
    profile = MemoryProfile(root, bot_id)

    # ── 固定前缀（缓存命中区）─────────────────────────
    profile_summary = profile.prompt_summary()
    system_parts = [system_prompt]
    if profile_summary:
        system_parts.append(f"\n[画像] {profile_summary}")
    system_msg = "\n".join(system_parts)

    # 订单上下文框架（结构稳定，内容动态）
    order_block = _format_order_context(order_context)

    # recent_events 内容
    recent_summaries = journal.read_recent_summaries(n_recent)
    recent_block = _format_recent(recent_summaries)

    # ── 动态后缀 ──────────────────────────────────────
    snapshot_block = _format_snapshot(snapshot)

    # ── 消息列表（OpenAI/DeepSeek chat format）────────
    user_content = f"""[订单上下文]
{order_block}

[近况]
{recent_block}

[本轮快照]
{snapshot_block}

请输出 Plan JSON（含 memory_refs 引用历史决策）。"""

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content},
    ]

    return {
        "messages": messages,
        "order_context": order_context,
        "recent_summaries": recent_summaries,
        "profile_summary": profile_summary,
    }


def _format_order_context(ctx: Optional[dict]) -> str:
    if not ctx:
        return "（当前无持仓）"
    lines = [
        f"订单: {ctx.get('order_id', '')}",
        f"标的: {ctx.get('symbol', '')}  方向: {ctx.get('side', '')}",
        f"入场: {ctx.get('entry_price', '?')}  TP: {ctx.get('tp', '-')}  SL: {ctx.get('sl', '-')}",
        f"理由: {ctx.get('reason', '')}",
    ]
    events = ctx.get("recent_events") or []
    if events:
        lines.append("关键事件:")
        for e in events:
            lines.append(f"  [{e.get('act', '')}] {e.get('detail', '')}")
    inv = ctx.get("invalidation") or []
    if inv:
        lines.append(f"失效标记: {len(inv)} 条")
    return "\n".join(lines)


def _format_recent(summaries: list[dict]) -> str:
    if not summaries:
        return "（无近期决策）"
    return "\n".join(
        f"{s.get('cycle_id', '')}: {s.get('decision', '')} — {s.get('reasoning', '')}"
        for s in summaries
    )


def _format_snapshot(snapshot: dict) -> str:
    """快照格式化（殿后，不追求缓存）。"""
    parts = []
    for k in ("market", "account", "position", "open_orders"):
        if k in snapshot:
            parts.append(f"{k}: {json.dumps(snapshot[k], ensure_ascii=False, default=str)}")
    return "\n".join(parts) if parts else json.dumps(snapshot, ensure_ascii=False, default=str)
