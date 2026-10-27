"""account.db 运维：每日定时清理 + 阈值告警字段（不改写入逻辑）。

背景（实测 2026-10-27）
- `position_history` 24 天累积 92 万行、单库 83.9MB；按 90 天保留稳态约 345 万行 / 315MB。
- 旧 `cleanup_account_data()` **只在进程启动时调用一次** —— 长跑进程永远不清理。
- 且清理表清单漏了 `price_order_history`。

本模块只做两件事（方案 §4.7）：
1. `cleanup_account_db()`：按 `timestamp` 分批删除超期行（含 price_order_history），可 dry-run；
2. `account_db_health()`：给出 rows / oldest_days / size 与是否越阈值，供 health 端点输出。
**不改 `save_position_history()` 的写入逻辑。**
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

__all__ = ["ACCOUNT_TABLES", "DEFAULT_RETENTION_DAYS", "cleanup_account_db",
           "account_db_health", "THRESHOLDS"]

DEFAULT_RETENTION_DAYS = 90
ACCOUNT_TABLES = (
    "balance_history",
    "position_history",
    "order_history",
    "trade_history",
    "price_order_history",   # 旧实现漏掉这张 → 只增不减
)

THRESHOLDS = {
    "size_mb": 500,
    "rows": 3_500_000,
    "oldest_days": 120,
    "disk_free_gb": 5,
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def cleanup_account_db(conn: sqlite3.Connection, days: int = DEFAULT_RETENTION_DAYS,
                       now: Optional[float] = None, batch: int = 10_000,
                       dry_run: bool = False) -> dict:
    """分批删除超期历史行（每批一个事务，单次锁 <50ms）。

    返回 ``{"deleted": {table: n}, "total": n, "cutoff": ts}``。
    """
    now = time.time() if now is None else now
    cutoff = int(now - max(1, int(days)) * 86400)
    present = _tables(conn)
    deleted: dict[str, int] = {}
    for table in ACCOUNT_TABLES:
        if table not in present:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()}
        if "timestamp" not in cols:
            continue
        if dry_run:
            n = conn.execute(
                f"SELECT count(*) FROM [{table}] WHERE timestamp < ?", (cutoff,)).fetchone()[0]
            deleted[table] = int(n)
            continue
        total = 0
        while True:
            with conn:
                cur = conn.execute(
                    f"DELETE FROM [{table}] WHERE rowid IN ("
                    f"  SELECT rowid FROM [{table}] WHERE timestamp < ? LIMIT ?)",
                    (cutoff, int(batch)))
            n = int(cur.rowcount or 0)
            total += n
            if n < batch:
                break
        deleted[table] = total
    return {"deleted": deleted, "total": sum(deleted.values()), "cutoff": cutoff}


def account_db_health(conn: sqlite3.Connection, size_mb: Optional[float] = None,
                      disk_free_gb: Optional[float] = None,
                      now: Optional[float] = None) -> dict:
    """行数 / 最老天数 / 体积 + 越阈值告警（不参与 is_system_healthy，避免重启风暴）。"""
    now = time.time() if now is None else now
    present = _tables(conn)
    rows = 0
    oldest_days = 0.0
    per_table: dict[str, int] = {}
    for table in ACCOUNT_TABLES:
        if table not in present:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()}
        n = conn.execute(f"SELECT count(*) FROM [{table}]").fetchone()[0]
        per_table[table] = int(n)
        rows += int(n)
        if "timestamp" in cols:
            lo = conn.execute(f"SELECT min(timestamp) FROM [{table}]").fetchone()[0]
            if lo:
                oldest_days = max(oldest_days, (now - float(lo)) / 86400.0)
    warnings: list[str] = []
    if size_mb is not None and size_mb > THRESHOLDS["size_mb"]:
        warnings.append(f"account_db_size:{size_mb:.0f}MB>{THRESHOLDS['size_mb']}MB")
    if rows > THRESHOLDS["rows"]:
        warnings.append(f"account_db_rows:{rows}>{THRESHOLDS['rows']}")
    if oldest_days > THRESHOLDS["oldest_days"]:
        warnings.append(f"account_db_oldest:{oldest_days:.0f}d>{THRESHOLDS['oldest_days']}d")
    if disk_free_gb is not None and disk_free_gb < THRESHOLDS["disk_free_gb"]:
        warnings.append(f"disk_free:{disk_free_gb:.1f}GB<{THRESHOLDS['disk_free_gb']}GB")
    return {
        "rows": rows,
        "per_table": per_table,
        "oldest_days": round(oldest_days, 1),
        "size_mb": round(size_mb, 1) if size_mb is not None else None,
        "disk_free_gb": round(disk_free_gb, 1) if disk_free_gb is not None else None,
        "warnings": warnings,
    }
