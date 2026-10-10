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
    n_index: int = 20,
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
    # 画像**不放在这里**：它每平一笔就变（`record_trade` / 账本投影），放进 system
    # 会让整段缓存前缀失效 —— 而 system 是最长的一段（人设+契约+工具）。
    # 实测缓存命中率 0.04–0.75、单轮 70–331k token，前缀稳定性是成本的第一杠杆。
    system = system_prompt

    # ── 动态后缀（缓存未命中区）───────────────────────
    profile_summary = profile.prompt_summary()
    order_block = _format_order_context(order_context)
    # 只读一次 journal：原先 `read_recent_summaries` 与 `read_recent(1)` 各做一次
    # `read_text()`，每轮多读一遍整个文件（独立评审指出）。
    # 一次读够索引层需要的条数，再切片 —— 摘要层与索引层共用这一份。
    rows = journal.read_recent(max(n_recent, n_index))
    # `n_recent=0` 时 `rows[-0:]` 会返回**全部**行（不是 0 行）—— 显式挡掉这个 -0 陷阱
    recent_rows = rows[-n_recent:] if (rows and n_recent > 0) else []
    recent_summaries = [
        {"cycle_id": r.get("cycle_id", ""), "decision": r.get("decision", ""),
         # 按币（多币下 `decision` 只是不带币名的动作串，渲染层要看这两个：
         # `_decision_text` 靠它们才能说出「哪个币做了什么」）
         "symbols": r.get("symbols"), "decisions": r.get("decisions"),
         # **不再 [:30] 截断**：模型每轮写 6K–76K 字符的推理，而 `[近况]` 原先只
         # 回看到 30 字（实测整段 171 字符）。journal 写入侧已截到 `[:200]`，
         # 是这里又砍了一刀。
         "reasoning": str(r.get("reasoning") or ""),
         # 执行结果：只给「下出去几张」这一个数，够判断上轮意图是否生效
         "exec_result": r.get("exec_result") or {}}
        for r in recent_rows
    ]
    recent_block = _format_recent(recent_summaries)
    index_block = _format_index(rows[-n_index:] if n_index else [])
    last_state = _format_last_plan_state(recent_rows[-1:])
    last_state_block = f"\n[上轮方案状态]\n{last_state}\n" if last_state else ""
    snapshot_block = snapshot_text or _format_snapshot(snapshot or {})
    # 画像段：无成交时不写（别让模型看到「历史表现: 0笔」这种噪音）
    profile_block = f"[画像] {profile_summary}\n\n" if profile_summary else ""

    user = f"""{profile_block}[订单上下文]
{order_block}

[近况]
{recent_block}
{index_block}
{last_state_block}
[本轮快照]
{snapshot_block}"""
    # 收尾指令由 `snapshot_text` 自带（`build_user_prompt` 的末尾）。两处都写会连着
    # 出两句几乎一样的「请输出 Plan JSON」——实测 2026-10-06 的 user prompt 就是
    # 「请输出本轮 Plan JSON。\n\n请输出 Plan JSON。」。
    # 判据用**调用方有没有给渲染好的快照**，不用子串探测：快照正文里恰好出现
    # 「Plan JSON」字样时，子串探测会静默吞掉兜底收尾句（独立评审实测过这条路径）。
    if not snapshot_text:
        user += "\n\n请输出 Plan JSON（含 memory_refs 引用历史决策）。"
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


# 订单记忆为空时的文案。**不能说「当前无持仓」** —— 那是关于**持仓**的断言，而这一段
# 只知道「有没有订单记录」。实测 brooks-btc 持 -56 BTC 时，prompt 里照样写着
# 「[订单上下文]（当前无持仓）」：一句错话，而模型只能靠契约规则 16 去仲裁。
# 现在改成如实说明，并顺手指向权威来源（快照的 position_state）。
_NO_ORDER_CTX = "（无订单记录 —— 持仓状态以快照 account.position_state 为准）"


def _format_order_context(ctx: Optional[dict]) -> str:
    if not ctx:
        return _NO_ORDER_CTX
    # 多币：按币各一张（`_order_context_for` 的容器形态）。合并成一张会让模型以为
    # 只有一笔持仓，其余币的前提失效价/理由全看不到（D-19）—— 而「上一轮我给 ETH
    # 定的失效价是多少」正是这一轮该照着判断的东西。
    many = ctx.get("orders") if isinstance(ctx, dict) else None
    if isinstance(many, list):
        parts = [_format_one_order(c) for c in many if isinstance(c, dict)]
        return "\n\n".join(parts) if parts else _NO_ORDER_CTX
    return _format_one_order(ctx)


def _format_one_order(ctx: dict) -> str:
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


def _exec_tag(row: dict) -> str:
    """执行标记：下出去几个单就标 `[orders=N]`，没下单不标。

    **为什么需要**：`[近况]` 与索引原先只给 `decision`，执行结果只躺在 journal 的
    `exec_result` 里。快照里的挂单能间接反映「成功」的情况，但**被拒的**（例如
    `TRIGGER_PRICE_SIDE: tp=... 需 > mark ...`）完全看不出来 —— 模型会把上一轮的
    意图当成已生效，继续在错误的前提上推理。

    **为什么写成 `[orders=N]` 而不是更短的 `[+N]`**：实测 `[+1]` 会被误读成仓位大小
    （「The past cycles opened size +1?」），模型得自己纠正过来。`orders=N` 无歧义，
    而且模型本来就用英文思考。N 是**订单条数**，不是张数。

    没下单时不标（而不是标 `[orders=0]`）：hold 轮占绝大多数，每行拖一个零标记只是
    噪音。
    """
    ex = row.get("exec_result") or {}
    try:
        n = int(ex.get("orders") or 0)
    except (TypeError, ValueError):
        n = 0
    return f" [orders={n}]" if n else ""


def _decision_text(r: dict) -> str:
    """决策的渲染文本。

    单币（或老 journal 没有 `symbols` 那些键时）**原样返回 `decision`** —— 单币 prompt
    必须逐字不变（I11）。多币下 `decision` 只是不带币名的动作串
    （`"hold,open_long"` 看不出哪个币做了什么），所以按 `symbols`/`decisions` 渲染成
    `BTC_USDT:hold ETH_USDT:open_long`。
    """
    symbols = r.get("symbols")
    decisions = r.get("decisions")
    if (isinstance(symbols, list) and isinstance(decisions, list)
            and len(symbols) > 1 and len(symbols) == len(decisions)):
        pairs = [f"{s}:{d}" for s, d in zip(symbols, decisions) if s]
        if pairs:
            return " ".join(pairs)
    return str(r.get("decision") or "")


def _tier1_bits(d: dict) -> list:
    """Tier 1 片段（单币读顶层字段、多币读 `tier1[symbol]` —— 同一套字段名）。"""
    bits = []
    if d.get("region"):
        bits.append(f"区域={d['region']}")
    if d.get("invalidation_price") is not None:
        bits.append(f"前提失效={d['invalidation_price']}（触及即视为结构破坏，须撤单或离场）")
    if d.get("time_stop_bars") is not None:
        bits.append(f"最大持仓={d['time_stop_bars']} 轮")
    if d.get("give_back_pct") is not None:
        bits.append(f"浮盈回撤阈值={d['give_back_pct']}%")
    return bits


def _format_recent(summaries: list[dict]) -> str:
    if not summaries:
        return "（无近期决策）"
    return "\n".join(
        f"{s.get('cycle_id', '')}: {_decision_text(s)}{_exec_tag(s)}"
        f" — {s.get('reasoning', '')}"
        for s in summaries
    )


def _format_index(rows: list[dict]) -> str:
    """最近 N 轮的一行索引（`cycle_id` + `decision` + 执行标记），不含推理正文。

    **为什么要有这一层**：把 N 轮正文都塞进 prompt 是**线性成本**，而模型有 148 小时
    的决策历史（实测 896 条 journal）。所以给**索引**而不是正文 —— 模型看得到
    「最近哪些轮做了什么」，需要某轮细节时用 `journal_lookup(cycle_id)` 按需取。

    这样段落体积不随历史增长，是它替代「回看 N 轮」的关键。
    """
    if not rows:
        return ""
    lines = "\n".join(
        f"  {r.get('cycle_id', '')} {_decision_text(r)}{_exec_tag(r)}" for r in rows
    )
    return (f"\n[近期决策索引·最近 {len(rows)} 轮"
            f"（要看某轮细节用 journal_lookup(cycle_id)）]\n{lines}")


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
    # 多币：每枚币各有自己的前提失效价/最大持仓 —— 合并成一句会让模型以为那些值是
    # 全局的（审计 D-22）。单币（tier1 只有一条或没有）走下面同一套顶层字段，逐字不变。
    tier1 = r.get("tier1")
    if isinstance(tier1, dict) and len(tier1) > 1:
        chunks = []
        for sym, one in tier1.items():
            bits = _tier1_bits(one if isinstance(one, dict) else {})
            if bits:
                chunks.append(f"{sym}: " + "；".join(bits))
        return "；".join(chunks)
    return "；".join(_tier1_bits(r))


def _format_snapshot(snapshot: dict) -> str:
    """快照格式化（殿后，不追求缓存）。"""
    parts = []
    for k in ("market", "account", "position", "open_orders"):
        if k in snapshot:
            parts.append(f"{k}: {json.dumps(snapshot[k], ensure_ascii=False, default=str)}")
    return "\n".join(parts) if parts else json.dumps(snapshot, ensure_ascii=False, default=str)
