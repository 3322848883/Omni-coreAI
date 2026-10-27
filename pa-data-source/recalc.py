"""序列级流式重算：`recalc_series()` —— 取代 `recalculate_all_ema()` 的整表读全量重算。

为什么要换
- 旧实现 `kline_watcher.recalculate_all_ema()` 把整条序列读进内存（5760 行 × 1.5KB ≈ 8.6MB）
  再逐行 UPDATE；校准只改了尾部 10 根也要全量重算（1015 行 vs 5760 行，白干 82%）。
- 新实现：**keyset 分页**（默认 500 行/批，常驻内存 ≈60KB）+ `StreamState` 增量，
  从 `from_t - warmup` 开始，只重算受影响区间。

一致性
- 与 `indicators_core` 批量函数逐位一致（同一 StreamState），不引入第二套口径；
- 只写**表里存在**的列（v1 库只有 ema20/atr14，v2 库按配置的列）。
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional, Sequence

import indicators_core as core

__all__ = ["recalc_series", "verify_series", "series_bounds"]

_CORE_COLS = ("t", "o", "h", "l", "c")


def series_bounds(conn: sqlite3.Connection, symbol: str, interval: str) -> tuple[Optional[int], Optional[int], int]:
    row = conn.execute(
        "SELECT min(t), max(t), count(*) FROM kline WHERE symbol=? AND interval=?",
        (symbol, interval),
    ).fetchone()
    return (row[0], row[1], int(row[2] or 0)) if row else (None, None, 0)


def _read_batch(conn, symbol, interval, after_t: Optional[int], limit: int,
                columns: Sequence[str]) -> list[sqlite3.Row]:
    cols = ", ".join(["t", *columns])
    if after_t is None:
        sql = f"SELECT {cols} FROM kline WHERE symbol=? AND interval=? ORDER BY t ASC LIMIT ?"
        args = (symbol, interval, limit)
    else:
        sql = (f"SELECT {cols} FROM kline WHERE symbol=? AND interval=? AND t>? "
               f"ORDER BY t ASC LIMIT ?")
        args = (symbol, interval, after_t, limit)
    return conn.execute(sql, args).fetchall()


def recalc_series(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    specs: Iterable,
    from_t: Optional[int] = None,
    batch: int = 500,
    dry_run: bool = False,
) -> dict:
    """从 ``from_t - warmup`` 起重算该序列的指标列。

    参数
    - specs：``indicator_config.IndicatorSpec`` 列表（决定算哪些列、预热多少）
    - from_t：只重算该时刻及之后（None = 全序列）
    - dry_run：只统计不写库

    返回 ``{"rows": 扫描行数, "updated": 写入行数, "missing": 缺失列, "start_t": 起点}``
    """
    specs = list(specs)
    spec_dicts = [s.as_dict() for s in specs]
    wanted = [c for s in specs for c in s.columns]
    existing = {r[1] for r in conn.execute("PRAGMA table_info([kline])").fetchall()}
    missing = [c for c in wanted if c not in existing]
    cols = [c for c in wanted if c in existing]
    if not cols:
        return {"rows": 0, "updated": 0, "missing": missing, "start_t": None}

    warmup = max((s.warmup for s in specs), default=0)
    start: Optional[int] = None
    if from_t is not None:
        # 取 from_t 之前最近的 warmup 根作为预热起点
        row = conn.execute(
            "SELECT t FROM kline WHERE symbol=? AND interval=? AND t<? "
            "ORDER BY t DESC LIMIT 1 OFFSET ?",
            (symbol, interval, int(from_t), max(warmup - 1, 0)),
        ).fetchone()
        start = int(row[0]) if row else None

    # 预热需要的 OHLC 也要读出来（只读 t/o/h/l/c，指标列不读）
    st = core.StreamState(spec_dicts)
    scanned = updated = 0
    cursor = start
    while True:
        rows = _read_batch(conn, symbol, interval, cursor, batch, _CORE_COLS[1:])
        if not rows:
            break
        payload = []
        for r in rows:
            t, o, h, l, c = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
            vals = st.push(o, h, l, c)
            scanned += 1
            if from_t is not None and t < int(from_t):
                continue  # 预热段只推状态，不写
            payload.append(tuple([vals.get(cn) for cn in cols] + [symbol, interval, t]))
        if payload and not dry_run:
            sets = ", ".join(f"[{c}]=?" for c in cols)
            sql = f"UPDATE kline SET {sets} WHERE symbol=? AND interval=? AND t=?"
            with conn:
                conn.executemany(sql, payload)
            updated += len(payload)
        elif payload:
            updated += len(payload)
        cursor = int(rows[-1][0])
        if len(rows) < batch:
            break
    return {"rows": scanned, "updated": updated, "missing": missing, "start_t": start}


def verify_series(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    specs: Iterable,
    tail: int = 200,
    rel_tol: float = 1e-9,
) -> dict:
    """独立校验：读库内已存值 vs 从全序列重算的尾部值，返回最大相对误差。"""
    specs = list(specs)
    spec_dicts = [s.as_dict() for s in specs]
    wanted = [c for s in specs for c in s.columns]
    existing = {r[1] for r in conn.execute("PRAGMA table_info([kline])").fetchall()}
    cols = [c for c in wanted if c in existing]
    if not cols:
        return {"n": 0, "max_rel": 0.0, "cols": []}
    total = conn.execute(
        "SELECT count(*) FROM kline WHERE symbol=? AND interval=?", (symbol, interval)
    ).fetchone()[0]
    if not total:
        return {"n": 0, "max_rel": 0.0, "cols": cols}

    # 全序列流式推进到最后 tail 根
    st = core.StreamState(spec_dicts)
    keep: list[tuple[int, dict]] = []
    cursor = None
    while True:
        rows = _read_batch(conn, symbol, interval, cursor, 500, _CORE_COLS[1:])
        if not rows:
            break
        for r in rows:
            t = int(r[0])
            vals = st.push(float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            keep.append((t, vals))
            if len(keep) > tail:
                keep.pop(0)
        cursor = int(rows[-1][0])
        if len(rows) < 500:
            break

    max_rel = 0.0
    worst = None
    n = 0
    for t, ours in keep:
        stored = conn.execute(
            f"SELECT {', '.join('[' + c + ']' for c in cols)} FROM kline "
            "WHERE symbol=? AND interval=? AND t=?",
            (symbol, interval, t),
        ).fetchone()
        if not stored:
            continue
        for c, ref in zip(cols, stored):
            mine = ours.get(c)
            if mine is None or ref is None:
                continue
            n += 1
            a, b = float(mine), float(ref)
            rel = 0.0 if a == b else abs(a - b) / max(abs(a), abs(b), 1e-12)
            if rel > max_rel:
                max_rel, worst = rel, (t, c, mine, ref)
    return {"n": n, "max_rel": max_rel, "worst": worst, "cols": cols,
            "ok": max_rel <= rel_tol}
