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
    order_context: Optional[dict] = None,
    snapshot: Optional[dict] = None,
    snapshot_text: str = "",
    n_recent: int = 3,
    extra_suffix: str = "",
) -> dict:
    """组装每轮主上下文。

    顺序按缓存优化（设计 S2.6）：**固定前缀在前、变化内容殿后**
    system（人设+契约+工具+画像） → 订单上下文 → recent_events → 快照 → 附加块。

    `snapshot_text` 非空时直接用它当「本轮快照」段（调用方已渲染好，例如带了
    风控/品种宇宙），否则由 `snapshot` 格式化而来 —— 两条路都不丢内容。

    返回 messages + 组装好的 system/user 字符串 + token 粗估。
    """
    journal = MemoryJournal(root, bot_id)
    profile = MemoryProfile(root, bot_id)

    # ── 固定前缀（缓存命中区）─────────────────────────
    profile_summary = profile.prompt_summary()
    system_parts = [system_prompt]
    if profile_summary:
        system_parts.append(f"\n[画像] {profile_summary}")
    system = "\n".join(system_parts)

    # ── 动态后缀（缓存未命中区）───────────────────────
    order_block = _format_order_context(order_context)
    # 只读一次 journal：原先 `read_recent_summaries` 与 `read_recent(1)` 各做一次
    # `read_text()`，每轮多读一遍整个文件（独立评审指出）。
    recent_rows = journal.read_recent(n_recent)
    recent_summaries = [
        {"cycle_id": r.get("cycle_id", ""), "decision": r.get("decision", ""),
         "reasoning": str(r.get("reasoning") or "")[:30]}
        for r in recent_rows
    ]
    recent_block = _format_recent(recent_summaries)
    last_state = _format_last_plan_state(recent_rows[-1:])
    last_state_block = f"\n[上轮方案状态]\n{last_state}\n" if last_state else ""
    snapshot_block = snapshot_text or _format_snapshot(snapshot or {})

    user = f"""[订单上下文]
{order_block}

[近况]
{recent_block}
{last_state_block}
[本轮快照]
{snapshot_block}

请输出 Plan JSON（含 memory_refs 引用历史决策）。"""
    if extra_suffix:
        user = user + "\n" + extra_suffix

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    return {
        "messages": messages,
        "system": system,
        "user": user,
        "order_context": order_context,
        "recent_summaries": recent_summaries,
        "profile_summary": profile_summary,
        "cacheable_prefix_tokens": _est_tokens(system),
        "dynamic_tokens": _est_tokens(user),
    }


def _est_tokens(text: str) -> int:
    """粗估 token 数：CJK 一字≈1 token，其余≈4 字符/token。

    只用于观测（对照设计 S2.6 的 ~4k/轮预算），不参与任何控制流。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk + 3) // 4


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
    # 模型声明的前提失效价（契约 Tier 1）—— 必须给**值**而不是计数：
    # 上一轮它自己写了「跌破 X 即视为结构破坏」，这一轮要能照着它判断，
    # 只报「N 条」等于没记。
    pi = ctx.get("premise_invalidation") or {}
    if pi.get("price") is not None:
        note = f" — {pi['note']}" if pi.get("note") else ""
        lines.append(f"前提失效: {pi['price']}（触及即视为结构破坏，须撤单或离场）{note}")
    return "\n".join(lines)


def _format_recent(summaries: list[dict]) -> str:
    if not summaries:
        return "（无近期决策）"
    return "\n".join(
        f"{s.get('cycle_id', '')}: {s.get('decision', '')} — {s.get('reasoning', '')}"
        for s in summaries
    )


def _format_last_plan_state(rows: list[dict]) -> str:
    """上一轮方案的状态位（契约 Tier 1）。

    **为什么单 bot 需要这一块**：`premise_invalidation` 走的是订单上下文，而订单记录
    只有 persona 组会写（`data/shared/orders/`）—— 单 bot（`plan-loop`）**从不写**。
    于是规则 18 要求模型填的 `invalidation`/`time_stop_bars`/`give_back_pct` 对
    brooks-btc 这类 bot 等于白填：下一轮看不到。所以这里从**决策日志**回看上一轮，
    让人格路径与单 bot 路径都能兑现「填了才有跨轮一致性」这句承诺。
    （实测来源：独立评审 Critical 3）
    """
    if not rows:
        return ""
    r = rows[-1]
    bits = []
    if r.get("region"):
        bits.append(f"区域={r['region']}")
    if r.get("invalidation_price") is not None:
        bits.append(f"前提失效={r['invalidation_price']}（触及即视为结构破坏，须撤单或离场）")
    if r.get("time_stop_bars") is not None:
        bits.append(f"最大持仓={r['time_stop_bars']} 轮")
    if r.get("give_back_pct") is not None:
        bits.append(f"浮盈回撤阈值={r['give_back_pct']}%")
    return "；".join(bits)


def _format_snapshot(snapshot: dict) -> str:
    """快照格式化（殿后，不追求缓存）。"""
    parts = []
    for k in ("market", "account", "position", "open_orders"):
        if k in snapshot:
            parts.append(f"{k}: {json.dumps(snapshot[k], ensure_ascii=False, default=str)}")
    return "\n".join(parts) if parts else json.dumps(snapshot, ensure_ascii=False, default=str)
