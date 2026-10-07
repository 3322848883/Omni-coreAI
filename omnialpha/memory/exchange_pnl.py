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

# 未归属记录的留痕上限（超出按 FIFO 丢弃最旧的）。
KEEP_EXCLUDED = 200


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
        "active_cursor": str(rec.get("active_cursor") or ""),
        "totals": {
            "trades": int(totals.get("trades") or 0),
            "wins": int(totals.get("wins") or 0),
            "pnl": float(totals.get("pnl") or 0.0),
            # `None` = 还没记过任何一笔（**不能拿 0 顶替**：那会让「最差单笔」
            # 在一串盈利里显示成 0，读起来像「有一笔不赚不亏」）
            "worst": None if worst is None else float(worst),
        },
        "fills": list(rec.get("fills") or []),
        # 未归属而被排除的记录（留痕，便于核对漏了什么）
        "excluded": list(rec.get("excluded") or []),
        "updated_at": int(rec.get("updated_at") or 0),
    }


def first_run_path(root: Path, bot_id: str) -> Path:
    return Path(root) / "data" / "bots" / str(bot_id) / "state" / "first_run.json"


def _journal_start_ts(root: Path, bot_id: str) -> int:
    """journal 首条记录的时间 —— **只在首次**用来定 `first_run_ts` 的起点。

    部署时 bot 往往已经跑了几天，若把起点写成「现在」，那几天的有效样本会被时间窗
    全部丢掉。首条 journal 是当时唯一可靠的运行起点。

    （**只在首次**用：之后以落盘值为准 —— journal 会被遗忘 GC 归档，首条会前移，
    一直跟着它会让时间窗逐月放宽。）
    """
    p = Path(root) / "data" / "bots" / str(bot_id) / "state" / "memory_journal.jsonl"
    if not p.is_file():
        return 0
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                return _int_or_zero((rec or {}).get("ts"))
    except Exception:  # noqa: BLE001
        return 0
    return 0


def ensure_first_run(root: Path, bot_id: str, *, now: Optional[int] = None) -> int:
    """返回并确保 `first_run_ts` 已落盘（首次调用写入，之后不再改）。

    **为什么单独一个文件、不放进 `exchange_pnl.json`**：那个文件会因为口径修正被
    删掉重建（本次就删了一次），而 `first_run_ts` 一旦重置成「现在」，时间窗就会
    把 bot 真实运行期的历史全部排除 —— 静默丢样本。

    **为什么不每次从 `memory_journal.jsonl` 首条取**：journal 会被遗忘 GC 按 TTL
    归档，首条会随归档前移，时间窗**逐月放宽**，历史污染悄悄回流。所以只在首次
    借它定起点，之后以落盘值为准。
    """
    p = first_run_path(root, bot_id)
    if p.is_file():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            v = int((rec or {}).get("first_run_ts") or 0)
            if v > 0:
                return v
        except Exception:  # noqa: BLE001
            pass
    ts = _journal_start_ts(root, bot_id) or int(now if now is not None else time.time())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"first_run_ts": ts}, ensure_ascii=False),
                     encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return ts


def local_realized_pnl(trades_log: Any) -> list[dict]:
    """从 `trades.jsonl` 提取 **bot 主动平仓**的已实现盈亏。

    这类平仓由 executor 发起，`detail.realized_pnl` 是它回传的权威值 —— 不必去
    交易所查，也**不会与 `position_close` 那条归属路径重叠**：那条只认
    `ao-<id>`（条件单触发），而主动平仓在 `position_close` 里是 `api`，会被判为
    未归属。所以两条路径天然互斥，不需要去重。

    返回 `[{"ts": int, "pnl": float, "cycle_id": str}]`，按 `ts` 升序。
    """
    out: list[dict] = []
    p = Path(trades_log)
    if not p.is_file():
        return out
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        for st in (rec.get("steps") or []):
            if not isinstance(st, dict):
                continue
            d = st.get("detail") or {}
            if not isinstance(d, dict):
                continue
            pnl = d.get("realized_pnl")
            if pnl is None:
                continue
            try:
                val = float(pnl)
            except (TypeError, ValueError):
                continue
            out.append({
                "ts": _parse_ts(rec.get("ts")),
                "pnl": round(val, 8),
                "cycle_id": str(rec.get("plan_cycle") or ""),
            })
    return out


def _parse_ts(v: Any) -> int:
    """`trades.jsonl` 的 `ts` 是 ISO 字符串（也可能已是 epoch 数字）→ int。"""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "").strip()
    if not s:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return 0


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


def _int_or_zero(v: Any) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _close_key(row: dict) -> Optional[int]:
    """平仓记录的游标键 —— `time_us`（微秒时间戳）。

    `position_close` **没有单调 id**（实测返回字段里只有 `time` 与 `time_us`），
    所以游标只能用微秒时间戳。它足够唯一，且随时间单调递增。

    **解析失败返回 None 而不是 0**：0 会被当成一个有效键参与 `> cursor` 比较，
    把坏数据混进游标。
    """
    v = row.get("time_us")
    if v is None:
        v = row.get("time")
        if v is None:
            return None
        try:
            return int(float(v) * 1_000_000)      # 秒 → 微秒，与 time_us 对齐量级
        except (TypeError, ValueError):
            return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _pnl_of(t: dict) -> Optional[float]:
    """该笔成交的已实现盈亏；取不到或为 0 都算「不是平仓」。"""
    try:
        v = float(t.get("pnl") or 0)
    except (TypeError, ValueError):
        return None
    return v if v != 0 else None


# ── 归属：哪些平仓是本 bot 自己的 ──────────────────────────────

_ID_CONTAINERS = ("order", "tp_placed", "sl_placed")
_ID_LISTS = ("tp_orders", "sl_orders")


def local_order_ids(trades_log: Any) -> set[str]:
    """从 `trades.jsonl` 收集本 bot 下过的所有订单 id（普通单 + 条件单）。

    **四个容器都要收**，漏一个就会让对应类型的单无法归属：

    - `detail.order.id` —— 普通单（开仓 / 主动平仓）
    - `detail.tp_placed.id` / `sl_placed.id` —— `modify_tp_sl` 路径
    - `detail.tp_orders[].id` / `sl_orders[].id` —— 开仓时随单挂的保护腿

    实测教训：只收 `order` + 两个 list 时本地 id 是 369 个，与交易所 `ao-<id>` 的
    **交集为 0**；补上 `tp_placed` 后变成 681 个、交集 8 个 —— 所有经 `modify_tp_sl`
    调整过的条件单此前都归不了属。

    文件缺失或某行损坏都**不抛**（返回已收集的部分）：归属是增强，不该让日志瑕疵
    把整轮同步打掉。
    """
    out: set[str] = set()
    p = Path(trades_log)
    if not p.is_file():
        return out
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        for st in (rec.get("steps") or []):
            if not isinstance(st, dict):
                continue
            d = st.get("detail") or {}
            if not isinstance(d, dict):
                continue
            for k in _ID_CONTAINERS:
                o = d.get(k)
                if isinstance(o, dict) and o.get("id"):
                    out.add(str(o["id"]))
            for k in _ID_LISTS:
                for leg in (d.get(k) or []):
                    if isinstance(leg, dict) and leg.get("id"):
                        out.add(str(leg["id"]))
    return out


def is_ours(text: Any, local_ids: set) -> bool:
    """`text` 形如 `ao-<id>` 且 `<id>` 在本地订单 id 里 → 这笔平仓是本 bot 的。

    **为什么只认这一条**：`position_close` 没有 `order_id`，`text` 又被交易所改写
    （bot 下单时的 `t-brk` 到这里变成 `ao-<id>` 或 `api`）。而 `ao-<id>` 的 id 实测
    就是**条件单 id**，它在本地日志里有记录。

    `api` / `-` 一律判否：它们没有唯一性，认它们等于把同账户其他来源的单也算进来。
    实测 `api` 用 (时间 ±180s, `accum_size == |size|`) 只能匹配 1/5，且 `accum_size`
    与单笔成交的 `size` 语义不同（35 vs 18）—— 模糊匹配不可靠，宁可判为未归属。
    """
    s = str(text or "")
    if not s.startswith("ao-"):
        return False
    return s[3:].strip() in local_ids


def _default_trades_log(root: Path, bot_id: str) -> Path:
    return Path(root) / "data" / "bots" / str(bot_id) / "logs" / "trades.jsonl"


def sync(root: Path, bot_id: str, client, *, contract: Optional[str] = None,
         contracts: Optional[list] = None, limit: int = 100,
         trades_log: Any = None, first_run_ts: Optional[int] = None) -> dict:
    """拉取平仓历史并幂等累积。返回本轮摘要。

    **只统计本 bot 自己的平仓**，两个来源合并：

    1. **SL/TP 触发** —— 交易所 `position_close` 里 `text == ao-<id>` 且 id 命中
       本地条件单 id（`is_ours`）。用官方 `pnl`（含手续费与资金费）。
    2. **bot 主动平仓** —— 本地 `trades.jsonl` 的 `detail.realized_pnl`（executor
       回传）。两条路径天然互斥：主动平仓在 `position_close` 里是 `api`，会被判为
       未归属，所以不需要去重。

    过滤顺序（**游标最先推进，任何过滤都不影响它**）：

    ```
    time_us > cursor → time >= first_run_ts → contract ∈ contracts → is_ours
    ```

    被排除的两类分别计数返回（`skipped_before_start` / `skipped_unattributed`），
    后者还落进 `excluded` 数组留痕 —— `api` 那部分本应由第 2 条来源覆盖，若那里
    有遗漏，`excluded` 是唯一能发现的地方。

    `contract` 透传给交易所（按合约过滤**请求**）；`contracts` 是**统计白名单**。
    两者分开是因为游标是**全账户**的（`position_close` 没有单调 id，只能用时间戳，
    而时间戳无法按 symbol 分段）。

    **拉取失败不动游标** —— 否则那一段会被永久跳过，比不拉更糟。
    """
    rec = load(root, bot_id)
    cursor = _int_or_zero(rec["cursor"])
    active_cursor = _int_or_zero(rec.get("active_cursor"))
    start = int(first_run_ts) if first_run_ts is not None else ensure_first_run(root, bot_id)

    log = Path(trades_log) if trades_log else _default_trades_log(root, bot_id)
    local_ids = local_order_ids(log)

    # ── 来源 2：bot 主动平仓（本地日志，executor 回传）──
    active = [x for x in local_realized_pnl(log) if x["ts"] > active_cursor]

    # ── 来源 1：SL/TP 触发（交易所）──
    try:
        rows = client.list_position_close(contract=contract, limit=limit) or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160], "added": 0,
                "trades": rec["totals"]["trades"]}

    allow = {str(c) for c in (contracts or []) if str(c).strip()} or None
    fresh: list[dict] = []
    excluded = list(rec.get("excluded") or [])
    skipped_unattr = 0
    skipped_before = 0
    max_key = cursor
    for t in rows:
        if not isinstance(t, dict):
            continue
        key = _close_key(t)
        if key is None:
            continue
        if key > max_key:
            max_key = key            # 游标在**所有**过滤之前推进
        if key <= cursor:
            continue
        ts = _int_or_zero(t.get("time"))
        if start and ts and ts < start:
            skipped_before += 1
            continue
        if allow and str(t.get("contract") or "") not in allow:
            continue
        if not is_ours(t.get("text"), local_ids):
            skipped_unattr += 1
            if len(excluded) < KEEP_EXCLUDED:
                excluded.append({
                    "text": str(t.get("text") or "")[:32],
                    "pnl": t.get("pnl"),
                    "contract": str(t.get("contract") or ""),
                    "time": t.get("time"),
                })
            continue
        pnl = _pnl_of(t)
        if pnl is None:
            continue
        fresh.append({
            "key": str(key),
            "pnl": round(pnl, 8),
            "contract": str(t.get("contract") or ""),
            "side": str(t.get("side") or ""),
            "time": t.get("time"),
            "source": "trigger",
        })

    for a in active:
        fresh.append({
            "key": str(a["ts"] * 1_000_000),
            "pnl": a["pnl"],
            "contract": "",
            "side": "",
            "time": a["ts"],
            "source": "active",
        })

    if not fresh:
        if max_key > cursor or active:
            rec["cursor"] = str(max_key)
            if active:
                rec["active_cursor"] = str(max(a["ts"] for a in active))
            rec["excluded"] = excluded[-KEEP_EXCLUDED:]
            _write(root, bot_id, rec)
        return {"ok": True, "added": 0, "added_active": 0,
                "skipped_unattributed": skipped_unattr,
                "skipped_before_start": skipped_before,
                "trades": rec["totals"]["trades"], "cursor": rec["cursor"]}

    fresh.sort(key=lambda x: _int_or_zero(x["key"]))
    t = rec["totals"]
    for f in fresh:
        t["trades"] += 1
        if f["pnl"] > 0:
            t["wins"] += 1
        t["pnl"] += f["pnl"]
        if t["worst"] is None or f["pnl"] < t["worst"]:
            t["worst"] = f["pnl"]
    rec["totals"] = t
    rec["cursor"] = str(max_key)
    if active:
        rec["active_cursor"] = str(max(a["ts"] for a in active))
    rec["fills"] = (rec["fills"] + fresh)[-KEEP_FILLS:]
    rec["excluded"] = excluded[-KEEP_EXCLUDED:]
    _write(root, bot_id, rec)
    n_active = sum(1 for f in fresh if f.get("source") == "active")
    return {"ok": True, "added": len(fresh), "added_active": n_active,
            "skipped_unattributed": skipped_unattr,
            "skipped_before_start": skipped_before,
            "trades": t["trades"], "cursor": rec["cursor"]}


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
