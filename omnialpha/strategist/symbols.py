"""symbol 参数的**唯一解析来源**（AI 工具与触发器共用）。

## 为什么单独成模块

这条判据原先在 6 个工具里各写一遍、且缺省值硬编码 `BTC_USDT`
（`tools.py:2426/2483/2537`、`tv_tools.py:320/349/658`）—— 于是**换币种/多币种**时
模型只要漏写一次 symbol，工具就**静默返回另一个币的数据**，不报错、不留痕，
而模型的整个判断就建立在错误标的上。

判据必须**一处定义、多处执行**（与 `trigger_store` 的 symbol 白名单同源）。
同一条规则在多个地方各写一遍，正是本仓反复踩到的失效形态。

## 规则

| 情形 | 行为 |
|---|---|
| 显式给了 `symbol`/`sym`，且在宇宙内 | 用它 |
| 显式给了，但**不在宇宙内** | **拒绝**（附宇宙列表）—— 越界不该被静默接受 |
| 没给，宇宙只有 1 个币 | **自动补**该币（唯一解，无歧义；实盘单币 bot 行为不变） |
| 没给，宇宙有 ≥2 个币 | **拒绝**（附宇宙列表）—— 不猜 |
| 没给，宇宙为空（未配置） | **拒绝**（提示未配置 symbols） |
| 没给，且调用方**未提供宇宙**（`None`，旧脚本/测试） | **拒绝**，但错误里说明「未提供宇宙」 |

**为什么不用「默认取宇宙第一个 + 提示」**：那会让模型拿**错币的数据**继续推理，
误导比多一次工具往返严重得多。
"""
from __future__ import annotations

from typing import Any, Optional

# 解析结果（note）
EXPLICIT = "explicit"                 # 显式给出且（有宇宙时）在宇宙内
AUTO_SINGLE = "auto_single"           # 宇宙唯一 → 自动补
OUT_OF_UNIVERSE = "out_of_universe"   # 显式给出但越界
AMBIGUOUS_MULTI = "ambiguous_multi"   # 多币宇宙且未指定
UNIVERSE_EMPTY = "universe_empty"     # 未配置 symbols
NO_UNIVERSE = "no_universe"           # 调用方没传宇宙（旧调用方）


def _norm(seq: Optional[list]) -> list[str]:
    out: list[str] = []
    for x in (seq or []):
        s = str(x or "").strip().upper()
        if s and s not in out:
            out.append(s)
    return out


def resolve_symbol_arg(
    args: Optional[dict], universe: Optional[list], *, tool: str = "",
) -> tuple[str, str]:
    """解析工具调用的 symbol → `(symbol, note)`；`symbol == ""` 表示**无法确定**。"""
    a = args or {}
    given = str(a.get("symbol") or a.get("sym") or "").strip().upper()
    uni = _norm(universe)
    if given:
        if uni and given not in uni:
            return "", OUT_OF_UNIVERSE
        return given, EXPLICIT
    if len(uni) == 1:
        return uni[0], AUTO_SINGLE
    if not uni:
        return "", (UNIVERSE_EMPTY if universe is not None else NO_UNIVERSE)
    return "", AMBIGUOUS_MULTI


def symbol_error_payload(note: str, universe: Optional[list], *, tool: str = "") -> dict:
    """拒绝时的返回体。**必须能自我纠正**：带上宇宙列表与下一步该怎么做。"""
    uni = _norm(universe)
    if note == OUT_OF_UNIVERSE:
        code, msg = "symbol_not_in_universe", "symbol 不在【品种宇宙】内"
    elif note == AMBIGUOUS_MULTI:
        code, msg = "symbol_required", "多币宇宙必须显式指定 symbol（不替你猜）"
    elif note == UNIVERSE_EMPTY:
        code, msg = "symbol_required", "本 bot 未配置 symbols，无法确定标的"
    else:  # NO_UNIVERSE
        code, msg = "symbol_required", "调用方未提供【品种宇宙】，无法确定标的"
    return {
        "error": code,
        "message": msg,
        "tool": tool,
        "universe": uni,
        "hint": "请在参数里显式给出 symbol（取值见 universe）",
    }


def auto_filled(note: str) -> bool:
    """是否发生了「自动补全」（用于审计：工具调用缺 symbol 率）。"""
    return note == AUTO_SINGLE
