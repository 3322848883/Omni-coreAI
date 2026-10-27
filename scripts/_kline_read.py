#!/usr/bin/env python3
"""统一的 kline 库只读访问（v1/v2 通用，显式列名，永不 `SELECT *`）。

为什么需要它：`scripts/` 里有 12 个脚本各自硬编码列名（有的还在用 `SELECT *`），
数据源升 v2（REAL + closed/src + 动态指标列）后会静默读错列。全部走这里：

    from _kline_read import connect_ro, columns, read_candles, iter_candles

- `connect_ro(db)`      → 只读 URI 连接（不会建 WAL、不可能加写锁）
- `columns(db)`         → 列清单（判断 v1/v2、是否有 closed）
- `read_candles(...)`   → 最近 N 根，升序返回 list[dict]，已过滤未收线
- `iter_candles(...)`   → keyset 分页迭代（回填/重算用，单批 ≤batch 根，内存可控）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator, Optional

CORE_COLUMNS = ("t", "o", "h", "l", "c", "v", "sum")


def connect_ro(db_path) -> sqlite3.Connection:
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(f"kline db missing: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def columns(db_path) -> list[str]:
    conn = connect_ro(db_path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info([kline])").fetchall()]
    finally:
        conn.close()


def _select_sql(cols: list[str], closed_only: bool) -> str:
    want = [c for c in CORE_COLUMNS if c in cols]
    extra = [c for c in cols if c not in CORE_COLUMNS and c not in ("symbol", "interval")]
    sql = "SELECT symbol, interval, " + ", ".join(want + extra) + " FROM kline"
    if closed_only and "closed" in cols:
        sql += " WHERE symbol=? AND interval=? AND closed=1"
    else:
        sql += " WHERE symbol=? AND interval=?"
    return sql


def _row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    for k, v in list(out.items()):
        if k in ("symbol", "interval"):
            continue
        if v is None:
            out[k] = None
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    if out.get("t") is not None:
        out["t"] = int(out["t"])
    return out


def read_candles(db_path, symbol: str, interval: str, limit: int = 200,
                 closed_only: bool = True) -> list[dict]:
    """最近 `limit` 根（升序）。库为 v1（无 closed 列）时自动不加过滤。"""
    cols = columns(db_path)
    sql = _select_sql(cols, closed_only) + " ORDER BY t DESC LIMIT ?"
    conn = connect_ro(db_path)
    try:
        rows = conn.execute(sql, (symbol, interval, int(limit))).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in reversed(rows)]


def iter_candles(db_path, symbol: str, interval: str, start_t: Optional[int] = None,
                 batch: int = 500, closed_only: bool = True) -> Iterator[list[dict]]:
    """按 t 升序 keyset 分页；每次 yield 一批（≤batch 根），供流式重算/回填校验。"""
    cols = columns(db_path)
    base = _select_sql(cols, closed_only)
    conn = connect_ro(db_path)
    try:
        cursor = start_t - 1 if start_t is not None else None
        while True:
            if cursor is None:
                rows = conn.execute(
                    base + " ORDER BY t ASC LIMIT ?", (symbol, interval, int(batch))
                ).fetchall()
            else:
                rows = conn.execute(
                    base + " AND t > ? ORDER BY t ASC LIMIT ?",
                    (symbol, interval, cursor, int(batch)),
                ).fetchall()
            if not rows:
                return
            yield [_row_to_dict(r) for r in rows]
            cursor = rows[-1]["t"]
    finally:
        conn.close()


def series_list(db_path) -> list[tuple[str, str, int, int, int]]:
    """(symbol, interval, rows, min_t, max_t) —— 现状勘察用。"""
    conn = connect_ro(db_path)
    try:
        return [
            (r[0], r[1], int(r[2]), int(r[3]), int(r[4]))
            for r in conn.execute(
                "SELECT symbol, interval, count(*), min(t), max(t) FROM kline "
                "GROUP BY symbol, interval ORDER BY symbol, interval"
            ).fetchall()
        ]
    finally:
        conn.close()
