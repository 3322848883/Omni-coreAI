"""kline.db v2 结构管理：建表 / 版本探测 / 按配置动态加列 / meta 读写。

调用顺序（写入端启动时**必须**在启任何写线程之前完成）：
    init_v2(conn)                    # 建表（幂等）
    ensure_indicator_columns(conn, specs)   # 按配置补列（幂等，纯元数据操作）
    set_meta(conn, "indicator_spec_hash", spec_hash(specs))

设计约束
- **只加不删**：删列必须离线重建表；残留列只告警（每列 8B/行）。
- `ADD COLUMN` 一律可空、**不带 DEFAULT**（带 DEFAULT 会触发全表重写 + 排他锁）。
- 列数保护：>32 告警、>64 直接拒绝（宽表超过 64 列后单序列读取会跨页，收益转负）。
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

__all__ = [
    "SCHEMA_VERSION",
    "DDL_KLINE",
    "DDL_META",
    "MAX_INDICATOR_COLUMNS",
    "WARN_INDICATOR_COLUMNS",
    "init_v2",
    "detect_version",
    "ensure_indicator_columns",
    "get_meta",
    "set_meta",
]

SCHEMA_VERSION = 2
WARN_INDICATOR_COLUMNS = 32
MAX_INDICATOR_COLUMNS = 64

DDL_KLINE = """
CREATE TABLE IF NOT EXISTS kline (
    symbol   TEXT    NOT NULL,
    interval TEXT    NOT NULL,
    t        INTEGER NOT NULL,
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    v REAL NOT NULL, sum REAL NOT NULL,
    closed INTEGER NOT NULL DEFAULT 1,
    src    TEXT    NOT NULL DEFAULT 'ws',
    PRIMARY KEY (symbol, interval, t)
) WITHOUT ROWID
"""

DDL_META = """
CREATE TABLE IF NOT EXISTS kline_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID
"""

DDL_INDEX = "CREATE INDEX IF NOT EXISTS idx_kline_time ON kline(t)"


def init_v2(conn: sqlite3.Connection) -> None:
    """建 v2 表与 meta（幂等）。不动已有数据。"""
    conn.execute(DDL_KLINE)
    conn.execute(DDL_META)
    conn.execute(DDL_INDEX)
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)


def _columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info([kline])").fetchall()]


def detect_version(conn: sqlite3.Connection) -> Optional[int]:
    """有 `closed` 列 → 2；有 kline 表但无 `closed` → 1；无表 → None。"""
    cols = _columns(conn)
    if not cols:
        return None
    return 2 if "closed" in cols else 1


def ensure_indicator_columns(conn: sqlite3.Connection, specs: Iterable) -> list[str]:
    """按 spec 的列清单补列，返回**本次新增**的列。

    specs 元素需有 ``columns`` 属性（``indicator_config.IndicatorSpec``）。
    """
    wanted: list[str] = []
    for spec in specs:
        wanted.extend(getattr(spec, "columns", ()) or ())
    if len(wanted) > MAX_INDICATOR_COLUMNS:
        raise ValueError(
            f"配置的指标列 {len(wanted)} 个，超过上限 {MAX_INDICATOR_COLUMNS}；"
            "宽表超过 64 列后单序列读取会跨页，请拆分指标集"
        )
    existing = set(_columns(conn))
    if not existing:
        raise RuntimeError("kline 表不存在：先调 init_v2()")
    missing = [c for c in wanted if c not in existing]
    if not missing:
        return []
    # 单事务；可空、无 DEFAULT → 纯元数据操作（11 列 <5ms，不重写数据页）
    with conn:
        for col in missing:
            conn.execute(f"ALTER TABLE kline ADD COLUMN [{col}] REAL")
    return missing


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM kline_meta WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO kline_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
