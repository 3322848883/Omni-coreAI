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

- `reconcile_protectors`：**保护单去重**（只裁同向同价的重复腿，保留不同触发价的
  多级 TP）—— 保护单堆积的引擎侧兜底
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


def _text(p: dict) -> str:
    init = p.get("initial") or {}
    return str(init.get("text") or p.get("text") or "")


def _tail(p: dict) -> str:
    t = _text(p)
    return t.rsplit("-", 1)[-1].lower() if t else ""


def _trigger_key(p: dict) -> Optional[str]:
    """触发价归一化后的键（「同价才算重复」判据）；缺字段 → None（互为重复）。

    刻意**不做 float() 转换**（那要 try/except，会给本模块加一条「裸吞异常」，
    撞审计 B12 棘轮）：只用字符串归一 —— 去空白、去尾零，
    于是 `"2408.5"` / `"2408.50"` / `2408.5` 归一到同一个键。
    """
    tri = p.get("trigger") or {}
    init = p.get("initial") or {}
    raw = tri.get("price") or init.get("trigger_price") or p.get("trigger_price")
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or None


#: TP 族「不同触发价」腿的保留上限（安全阀）。
#: 为什么需要：改判据后不同触发价的腿**一律保留**（多级止盈是策略本体），但若某个
#: bot 的目标价随行情漂移、每轮都在**新价位**挂一条，就会重新长成堆积。上限给到
#: 12 条 —— 阶梯类最多 10 档 + 马丁共用 1 条，留出余量；超出从**最旧**撤并留痕。
_MAX_DISTINCT_TP_LEGS = 12


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
    """保护单**去重**：保留不同触发价的多级结构，只裁「同向+同价+同文本」的重复腿。

    ## 为什么需要

    每轮重挂入场单都会各留一组 TP/SL，而 `executor._is_orphan_protector`
    只按**方向**判定（有同向持仓就保留、不看张数）—— 于是保护单**只增不减**。
    实测 eth-disc 在 6 小时里堆到 30 个 / 202 张，而持仓只有 4 张。

    另一个能对齐张数的函数 `_resync_protectors` **只在平/减仓路径被调用**，
    加仓路径不调 —— 所以这条路径完全没人管。

    ## 判据（2026-10-15 重写：不再按张数裁剪多级 TP）

    1. **SL（`sl`/`ls`）**：单层风控意图 —— 每方向只留**最新一条**，旧的撤掉
       （多价并存 = 提前平仓；与旧实现一致，零行为变化）。
    2. **TP（`tp`/`lp`）**：**按触发价保留** —— 不同触发价的多级止盈是策略本体，
       一律保留；只裁「同方向 + 同触发价 + 同文本」的**重复腿**，保留其中
       `|size|` 最大的一条（并列取最新）。张数**不做合并/重算**（覆盖对齐属于
       `_align_tp_group`；本函数只清重复，不改覆盖）。
    3. **安全阀**：不同触发价的 TP 腿条数上限 `_MAX_DISTINCT_TP_LEGS`，超出从
       **最旧**撤（防「目标价随行情漂移」时每轮在新价位攒一条）。

    ### 为什么改（实测根因，2026-10-15 11:40:55）

    旧判据是「同方向保护单按 `create_time` 倒序累加，凑够目标张数即停」，目标
    张数 = 持仓 + 待成交入场单。但 **SL 单张就覆盖整个目标**（`_sl_cover_cap`
    让 SL = 持仓 + 待成交），于是**只有与最新 SL 同 30 秒窗口的那条 TP 能活下来**：
    实测把马丁共用 TP 的三条腿（6/7/9 张）裁成一条（−9），并且会**周期性吃掉逐档
    止盈腿**（`ensure_grid_tp` 的做 T 腿）。根子是它**只按张数判超额、不看触发价**，
    与 `_align_tp_group` / `_resync_protectors` 的「**绝不归并不同触发价**」纪律
    直接冲突 —— 而逐档 TP 的全部价值就在「不同触发价」。

    **目标张数仍要算**（持仓 + 待成交入场单），但它只用于「既无持仓也无待成交
    → 不动」这条 fail-closed 闸门，不再用于裁剪多级结构。后者不能漏：入场单挂出后
    保护单就一起挂上了（那时还没持仓），只按持仓算会把预挂的保护单误判成
    孤儿撤掉 —— 委托一成交就是裸仓。

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

    # 判据见 docstring「## 判据」：① SL 每方向只留最新一条；② TP 按**触发价**
    # 保留、只裁同（方向+触发价+文本）的重复腿；③ 触发价种类超上限从最旧撤。
    kept: list[dict] = []
    dropped: list[dict] = []
    dedup = 0     # 同（方向+触发价+文本）的重复腿
    capped = 0    # 触发价种类超上限被撤（安全阀）

    sl_rows = [p for p in rows if _tail(p) in ("sl", "ls")]
    tp_rows = [p for p in rows if _tail(p) in ("tp", "lp")]

    # ① SL：单层风控意图 —— 每方向只留最新一条（多价并存 = 提前平仓）
    sl_by_side: dict[str, list[dict]] = {}
    for p in sl_rows:
        sl_by_side.setdefault(_protector_side(p) or "-", []).append(p)
    for grp in sl_by_side.values():
        grp.sort(key=_created, reverse=True)
        kept.append(grp[0])
        dropped.extend(grp[1:])
        dedup += max(0, len(grp) - 1)

    # ② TP：**不同触发价一律保留**；同价同文本的多条 = 重复挂单 → 只留覆盖最大
    #    的那条（并列取最新）。张数不做合并/重算（覆盖对齐是 `_align_tp_group` 的活）。
    tp_by_key: dict[tuple, list[dict]] = {}
    for p in tp_rows:
        tp_by_key.setdefault(
            (_protector_side(p) or "-", _trigger_key(p), _text(p)), []).append(p)
    for grp in tp_by_key.values():
        grp.sort(key=lambda q: (_protector_size(q), _created(q)), reverse=True)
        kept.append(grp[0])
        dropped.extend(grp[1:])
        dedup += max(0, len(grp) - 1)

    # ③ 安全阀：不同触发价的 TP 腿条数上限（防「目标价随行情漂移」时每轮攒一条）
    tp_kept = [p for p in kept if _tail(p) in ("tp", "lp")]
    if len(tp_kept) > _MAX_DISTINCT_TP_LEGS:
        tp_kept.sort(key=_created)                              # 最旧在前
        for p in tp_kept[:len(tp_kept) - _MAX_DISTINCT_TP_LEGS]:
            kept.remove(p)
            dropped.append(p)
            capped += 1

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

    tp_legs = len([p for p in kept if _tail(p) in ("tp", "lp")])
    if cancelled and getattr(executor, "alert_store", None) is not None:
        try:
            executor.alert_store.raise_alert(
                "orphan_protector",
                f"{symbol} 保护单重复腿已清理：保留 {len(kept)} 笔"
                f"（多级 TP 保留 {tp_legs} 条；撤重复 {dedup} 笔／超上限 {capped} 笔）"
                f"（持仓 {pos_size} + 待成交 {pending_size}）",
                symbol=symbol, kept=len(kept), cancelled=len(cancelled),
                position_size=pos_size, pending_size=pending_size,
                dedup=dedup, capped=capped, tp_legs=tp_legs,
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True, "symbol": symbol, "position_size": pos_size,
        "pending_size": pending_size, "target_size": target_size,
        "kept": len(kept), "cancelled": cancelled,
        "dedup": dedup, "capped": capped, "tp_legs": tp_legs,
    }

