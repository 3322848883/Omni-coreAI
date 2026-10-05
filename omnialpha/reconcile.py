"""对账：以交易所为唯一权威，把本地派生视图拉回来。

## 为什么需要（2026-10-05 架构盘点 S4/S7）

同一笔交易的状态至少有 9 份副本（交易所 REST / 执行内快照 / 共享订单库 /
台账 / 成交日志 / 记忆 journal / 记忆 profile / paper account.db / AI 快照），
**没有单一权威声明，也没有任何系统级对账** —— 唯一的 `reconcile_protection()`
只返回警告、生产调用点为零（唯一调用者是测试脚本）。

## 设计原则（来自外部实践，见 research/trading-system-design-upgrade/）

1. **交易所是权威，本地是缓存**（NautilusTrader）：修正方向**只能**是
   交易所 → 本地，绝不能反向。
2. **fail-closed**：取不到交易所报告时标记 `unknown`，**绝不当作「空仓」**；
   unknown 时不做任何破坏性动作（不撤单、不平仓）。
3. **宁可多留，不可误撤**：任何判据拿不准就保留保护单（撤错了是裸仓）。

## 本模块目前覆盖

- `reconcile_protectors`：**保护单张数对齐持仓**（保护单堆积的引擎侧兜底）
- `account_truth`：拉一份「交易所真相」快照供调用方比对
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def account_truth(executor: Any, symbol: str = "") -> dict:
    """拉一份交易所侧的真相快照。

    返回 `{"ok": bool, "positions": [...], "protectors": [...], "error": str}`。
    `ok=False` 表示**取数失败** —— 调用方必须按 unknown 处理（fail-closed），
    不得把它当成「空仓」。
    """
    out: dict = {"ok": False, "positions": [], "protectors": [], "error": ""}
    try:
        positions = executor._symbol_positions(symbol) if symbol else (
            executor.client.get_positions() or [])
        out["positions"] = list(positions or [])
    except Exception as e:  # noqa: BLE001
        out["error"] = f"positions: {e}"
        return out
    try:
        rows = executor.client.list_price_orders(symbol or None) or []
    except Exception as e:  # noqa: BLE001
        out["error"] = f"price_orders: {e}"
        return out
    out["protectors"] = [p for p in rows if executor._order_is_reduce_only(p)]
    out["ok"] = True
    return out


def _protector_size(p: dict) -> int:
    init = p.get("initial") or {}
    try:
        return abs(int(float(init.get("size") or p.get("size") or 0)))
    except (TypeError, ValueError):
        return 0


def _protector_side(p: dict) -> str:
    """保护单方向：负 size = 平多（long），正 = 平空（short）。"""
    init = p.get("initial") or {}
    try:
        sz = float(init.get("size") or p.get("size") or 0)
    except (TypeError, ValueError):
        return ""
    return "long" if sz < 0 else ("short" if sz > 0 else "")


def _is_tp_sl(p: dict) -> bool:
    init = p.get("initial") or {}
    text = str(init.get("text") or p.get("text") or "")
    tail = text.rsplit("-", 1)[-1].lower() if text else ""
    return tail in ("tp", "sl", "lp", "ls")


def _pending_entry_size(executor: Any, symbol: str) -> int:
    """待成交入场单的**张数合计**（保护单也该覆盖它们）。

    入场单挂出后 1-3 秒保护单就一起挂上了 —— 此时还没持仓。所以
    「保护单该覆盖多少」= 持仓张数 + 待成交入场单张数。

    **必须同时扫两类**：普通挂单（`limit`）与**条件单**里的入场类
    （`stop_entry_*` 挂在 `/price_orders`）。只扫普通单会漏掉突破进场单 ——
    实测 eth-disc 的入场单正是条件单，漏扫导致 `pending_size=0`、
    对账直接跳过（保护单 30 张 vs 目标 0）。

    这个坑在本项目里出现过三次（见 AGENTS.md 关于 `_has_pending_entry`
    的两条记录），所以这里两类都扫，并有测试钉住。
    """
    total = 0
    # 1) 普通挂单（limit 未成交的入场单）
    try:
        rows = executor._owned_open_orders(symbol, executor.label_prefix) or []
    except Exception:  # noqa: BLE001
        rows = []
    for o in rows:
        if o.get("is_reduce_only"):
            continue
        left = o.get("left")
        if left is not None and int(left) == 0:
            continue
        try:
            total += abs(int(float(o.get("size") or 0)))
        except (TypeError, ValueError):
            continue
    # 2) 条件单里的入场类（stop_entry_*，非 reduce_only、未终结）
    try:
        conds = executor._owned_price_orders(symbol, executor.label_prefix) or []
    except Exception:  # noqa: BLE001
        conds = []
    for p in conds:
        try:
            if executor._order_is_reduce_only(p):
                continue
        except Exception:  # noqa: BLE001
            continue
        status = str(p.get("status") or "").lower()
        if status in ("cancelled", "finished", "filled", "triggered", "failed", "closed"):
            continue
        init = p.get("initial") or {}
        try:
            total += abs(int(float(init.get("size") or p.get("size") or 0)))
        except (TypeError, ValueError):
            continue
    return total


def reconcile_protectors(executor: Any, symbol: str) -> dict:
    """把该 symbol 的保护单**张数对齐到「持仓 + 待成交入场单」**（保留最新一组）。

    ## 为什么需要

    每轮重挂入场单都会各留一组 TP/SL，而 `executor._is_orphan_protector`
    只按**方向**判定（有同向持仓就保留、不看张数）—— 于是保护单**只增不减**。
    实测 eth-disc 在 6 小时里堆到 30 个 / 202 张，而持仓只有 4 张。

    另一个能对齐张数的函数 `_resync_protectors` **只在平/减仓路径被调用**，
    加仓路径不调 —— 所以这条路径完全没人管。

    ## 判据

    同方向保护单按 `create_time` **倒序**累加，凑够目标张数即停：
    已累加的那几笔保留（它们覆盖当前敞口），其余撤掉。

    **目标张数 = 持仓张数 + 待成交入场单张数**。后者不能漏：入场单挂出后
    保护单就一起挂上了（那时还没持仓），只按持仓算会把预挂的保护单误判成
    超额撤掉 —— 委托一成交就是裸仓。

    ## fail-closed

    - 取不到交易所报告 → **不动任何单**
    - 目标张数为 0（既无持仓也无待成交入场单）→ **不动**（交给
      `_cleanup_orphan_protectors`，那才是「全无对应」的孤儿）
    """
    if not symbol:
        return {"ok": False, "error": "symbol required"}
    truth = account_truth(executor, symbol)
    if not truth["ok"]:
        return {"ok": False, "error": truth["error"], "cancelled": []}

    pos_size = 0
    for p in truth["positions"]:
        try:
            pos_size += abs(int(float(p.get("size") or 0)))
        except (TypeError, ValueError):
            continue
    pending_size = _pending_entry_size(executor, symbol)
    target_size = pos_size + pending_size
    if target_size <= 0:
        return {"ok": True, "skipped": "no_position", "cancelled": [],
                "position_size": pos_size, "pending_size": pending_size}

    rows = [p for p in truth["protectors"] if _is_tp_sl(p)]
    if executor.label_prefix:
        rows = [p for p in rows
                if executor._text_owned(
                    str((p.get("initial") or {}).get("text") or p.get("text") or ""),
                    executor.label_prefix)]

    def _created(p: dict) -> float:
        try:
            return float(p.get("create_time") or 0)
        except (TypeError, ValueError):
            return 0.0

    # **按「组」保留，不按「单」** —— 一次 `_open`/`_stop_entry` 会连续挂出
    # 一整组（TP 先、SL 后）。若按单倒序累加，最先被选中的是最新的那张 SL，
    # 凑够张数就停 → **把同组的 TP 撤掉**，保护不完整。
    #
    # 聚类用**时间窗**而不是「同一秒」：挂 TP 与 SL 是两次 API 调用，实测跨
    # 1-2 秒；同秒判据会把一组拆成两组，照样只留 SL 撤掉 TP（实测：账户上
    # 只剩 SL @2686，TP @2758 被自己的对账撤了）。
    #
    # 窗口取 **30 秒** —— 量级由两侧夹定：远大于「挂一组 TP+SL 的耗时」（秒级），
    # 远小于「两轮之间的间隔」（讨论组 17 分钟、单 bot 15 分钟）。
    _GROUP_WINDOW_SEC = 30.0
    ordered = sorted(rows, key=_created, reverse=True)
    ordered_groups: list[list[dict]] = []
    for p in ordered:
        if ordered_groups and abs(_created(p) - _created(ordered_groups[-1][0])) <= _GROUP_WINDOW_SEC:
            ordered_groups[-1].append(p)
        else:
            ordered_groups.append([p])

    kept: list[dict] = []
    dropped: list[dict] = []
    acc = 0
    for grp in ordered_groups:
        if acc < target_size:
            kept.extend(grp)
            acc += sum(_protector_size(p) for p in grp)
        else:
            dropped.extend(grp)

    cancelled: list[str] = []
    for p in dropped:
        pid = executor._order_id(p)
        if not pid:
            continue
        try:
            executor.client.cancel_price_order(pid)
            cancelled.append(pid)
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile: cancel %s failed: %s", pid, e)

    if cancelled and getattr(executor, "alert_store", None) is not None:
        try:
            executor.alert_store.raise_alert(
                "orphan_protector",
                f"{symbol} 保护单张数超额已对齐：保留 {len(kept)} 笔覆盖 {target_size} 张"
                f"（持仓 {pos_size} + 待成交 {pending_size}），撤掉 {len(cancelled)} 笔",
                symbol=symbol, kept=len(kept), cancelled=len(cancelled),
                position_size=pos_size, pending_size=pending_size,
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True, "symbol": symbol, "position_size": pos_size,
        "pending_size": pending_size, "target_size": target_size,
        "kept": len(kept), "cancelled": cancelled,
    }


def reconcile_bot(executor: Any, symbols: list[str]) -> dict:
    """对一组 symbol 跑保护单对账，返回汇总。"""
    out: dict = {"symbols": {}, "cancelled_total": 0}
    for sym in symbols or []:
        try:
            res = reconcile_protectors(executor, sym)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e), "cancelled": []}
        out["symbols"][sym] = res
        out["cancelled_total"] += len(res.get("cancelled") or [])
    return out
