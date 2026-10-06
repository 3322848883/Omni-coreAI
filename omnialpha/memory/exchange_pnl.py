"""从交易所成交明细投影已实现盈亏（live bot 的画像数据源）。

**为什么不能只靠本地日志**：SL/TP 触发是交易所侧成交、**没有本地信号** —— 既不进
`logs/trades.jsonl`，也不进 `data/shared/receipts/`。实测某 bot 的 339 行成交日志里
只有 3 条带 `realized_pnl`、83 条执行回执里只有 4 条。用它投影会漏掉**全部止损**，
而止损恰是负面样本 → 胜率被系统性抬高（`profile.py` 记过一次同类事故：459 笔真实
平仓只记了 1 笔，漏掉的正好包含止损）。

所以数据源只能是交易所的 `my_trades`。本模块按**游标幂等**摄取：只处理
`id > cursor` 且 `pnl != 0` 的成交，累积落 `state/exchange_pnl.json`。

统计形状与 `paper/store.py::realized_pnl_stats` 一致（`trades/wins/pnl/worst`），
所以 `MemoryProfile.ledger_stats` 能把两者当同一个数据源用。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

# 保留的明细条数（供排查用）。统计走 totals，不受这个上限影响。
KEEP_FILLS = 200


def _path(root: Path, bot_id: str) -> Path:
    return Path(root) / "data" / "bots" / str(bot_id) / "state" / "exchange_pnl.json"


def load(root: Path, bot_id: str) -> dict:
    """读投影状态。文件缺失/损坏返回空骨架，**不抛**。"""
    p = _path(root, bot_id)
    rec: Any = {}
    if p.exists():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rec = {}
    if not isinstance(rec, dict):
        rec = {}
    totals = rec.get("totals") or {}
    worst = totals.get("worst")
    return {
        "cursor": str(rec.get("cursor") or ""),
        "totals": {
            "trades": int(totals.get("trades") or 0),
            "wins": int(totals.get("wins") or 0),
            "pnl": float(totals.get("pnl") or 0.0),
            # `None` = 还没记过任何一笔（**不能拿 0 顶替**：那会让「最差单笔」
            # 在一串盈利里显示成 0，读起来像「有一笔不赚不亏」）
            "worst": None if worst is None else float(worst),
        },
        "fills": list(rec.get("fills") or []),
        "updated_at": int(rec.get("updated_at") or 0),
    }


def stats(root: Path, bot_id: str) -> Optional[dict]:
    """汇总成与 `realized_pnl_stats` 同形的统计。

    返回 `None` 表示**一笔都没有** —— 与 paper 侧的语义一致，调用方据此退回
    非账本口径。
    """
    t = load(root, bot_id)["totals"]
    if t["trades"] <= 0:
        return None
    return {
        "trades": t["trades"],
        "wins": t["wins"],
        "pnl": round(t["pnl"], 2),
        "worst": round(t["worst"] if t["worst"] is not None else 0.0, 2),
    }


def _trade_id(raw: Any) -> Optional[int]:
    """成交 id 归一化成 int（Gate 返回的是字符串数字）。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _pnl_of(t: dict) -> Optional[float]:
    """该笔成交的已实现盈亏；取不到或为 0 都算「不是平仓」。"""
    try:
        v = float(t.get("pnl") or 0)
    except (TypeError, ValueError):
        return None
    return v if v != 0 else None


def sync(root: Path, bot_id: str, client, *, contract: Optional[str] = None,
         limit: int = 1000) -> dict:
    """拉取成交明细并幂等累积。返回本轮摘要。

    只处理 `id > cursor` 且 `pnl != 0` 的成交：`pnl == 0` 是开仓成交，不计入已实现
    盈亏（与 `paper/store.py` 的 `realised_pnl != 0` 口径一致）。

    **拉取失败不动游标** —— 否则那一段会被永久跳过，比不拉更糟。
    """
    rec = load(root, bot_id)
    cursor = _trade_id(rec["cursor"]) or 0
    try:
        rows = client.list_my_trades(contract=contract, limit=limit) or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160], "added": 0,
                "trades": rec["totals"]["trades"]}

    fresh: list[dict] = []
    max_id = cursor
    for t in rows:
        if not isinstance(t, dict):
            continue
        tid = _trade_id(t.get("id"))
        if tid is None:
            continue
        if tid > max_id:
            max_id = tid
        if tid <= cursor:
            continue
        pnl = _pnl_of(t)
        if pnl is None:
            continue
        fresh.append({
            "id": str(t.get("id")),
            "pnl": round(pnl, 8),
            "contract": str(t.get("contract") or ""),
            "create_time": t.get("create_time"),
        })

    if not fresh:
        # 没有新的平仓，也要把游标推过去 —— 否则每次重复扫同一段
        if max_id > cursor:
            rec["cursor"] = str(max_id)
            _write(root, bot_id, rec)
        return {"ok": True, "added": 0, "trades": rec["totals"]["trades"],
                "cursor": rec["cursor"]}

    fresh.sort(key=lambda x: _trade_id(x["id"]) or 0)
    t = rec["totals"]
    for f in fresh:
        t["trades"] += 1
        if f["pnl"] > 0:
            t["wins"] += 1
        t["pnl"] += f["pnl"]
        if t["worst"] is None or f["pnl"] < t["worst"]:
            t["worst"] = f["pnl"]
    rec["totals"] = t
    rec["cursor"] = str(max_id)
    rec["fills"] = (rec["fills"] + fresh)[-KEEP_FILLS:]
    _write(root, bot_id, rec)
    return {"ok": True, "added": len(fresh), "trades": t["trades"],
            "cursor": rec["cursor"]}


def _write(root: Path, bot_id: str, rec: dict) -> None:
    """原子落盘（写临时文件再 replace）。

    读侧（`ledger_stats` 在 plan-loop 进程里每轮调）与本模块的写侧是**两个进程**，
    直接覆写会让读侧有概率读到半个文件。
    """
    p = _path(root, bot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec["updated_at"] = int(time.time())
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
