"""kline.db schema 契约（pa-data-source 写 → OmniAlpha 只读）。

版本语义
- **v1**：11 列，`o/h/l/c/v/sum` 为 TEXT，只带 `ema20`/`atr14` 两个指标列；
- **v2**：`o/h/l/c/v/sum` 为 REAL，新增 `closed`/`src`，指标列由 `watchlist.indicators`
  动态决定（不再强制 `ema20`/`atr14` 存在），主键 `(symbol, interval, t)` + `WITHOUT ROWID`。

兼容策略
- 读取端**只需要** `t/symbol/interval/o/h/l/c/v/sum` 九列就可用（REQUIRED_COLUMNS）；
  `ema20`/`atr14`/`closed`/`src` 及所有指标列一律按**可选**处理 → 缺了不影响整体可用性；
- `validate_kline_schema()` 返回**探测到的版本**（1 或 2），不再假定常量；
- `KLINE_SCHEMA_VERSION` 保留为「写端当前版本」，供写端自检与迁移脚本使用。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

#: 写端当前版本（读端不依赖它做判定，用探测结果）
KLINE_SCHEMA_VERSION = 2

#: 只读端的最小可用集：缺任一列即视为不可用
REQUIRED_COLUMNS = ("t", "symbol", "interval", "o", "h", "l", "c", "v", "sum")

#: v1 曾强制、v2 起改为可选的列
V1_ONLY_COLUMNS = ("ema20", "atr14")

#: v2 新增的元数据列
V2_META_COLUMNS = ("closed", "src")

#: 指标列命名（与 pa-data-source/indicator_config.py 的列名规范一致）
INDICATOR_COLUMN_RE = re.compile(
    r"^(?:ema|ma|sma|atr|rsi)\d+$"
    r"|^macd(?:_\d+_\d+_\d+)?(?:_dea|_hist)?$"
    r"|^boll\d+(?:_[\dpn]+)?(?:_upper|_lower)?$"
)


class SchemaError(Exception):
    pass


def table_columns(conn: sqlite3.Connection, table: str = "kline") -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info([{table}])").fetchall()]


def indicator_columns(columns) -> list[str]:
    """从列名集合里挑出指标列（保持传入顺序）。"""
    return [c for c in columns if INDICATOR_COLUMN_RE.match(str(c))]


def detect_version(columns) -> int:
    """有 `closed`（或 `src`）即判 v2，否则 v1。"""
    cols = {str(c) for c in columns}
    return 2 if (cols & set(V2_META_COLUMNS)) else 1


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def validate_kline_schema(db_path: Path) -> int:
    """校验并返回**探测到的**版本（1 或 2）；不满足最小集则抛 SchemaError。"""
    path = Path(db_path)
    if not path.exists():
        raise SchemaError(f"kline db missing: {path}")
    try:
        conn = _open(path)
        try:
            cols = table_columns(conn)
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise SchemaError(f"kline db unreadable: {e}") from e
    if not cols:
        raise SchemaError(f"kline table missing in {path}")
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise SchemaError(f"kline schema missing required columns: {missing} in {path}")
    return detect_version(cols)


def schema_ok(db_path: Optional[Path]) -> bool:
    if not db_path:
        return False
    try:
        validate_kline_schema(db_path)
        return True
    except SchemaError:
        return False


def schema_info(db_path: Optional[Path]) -> dict:
    """给诊断/监控用：版本、列清单、指标列、是否 v2 元数据齐备。"""
    if not db_path or not Path(db_path).exists():
        return {"ok": False, "error": "missing"}
    try:
        conn = _open(Path(db_path))
        try:
            cols = table_columns(conn)
        finally:
            conn.close()
    except sqlite3.Error as e:  # pragma: no cover
        return {"ok": False, "error": f"unreadable: {e}"}
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    return {
        "ok": not missing,
        "version": detect_version(cols) if not missing else None,
        "missing": missing,
        "columns": cols,
        "indicators": indicator_columns(cols),
        "has_closed": "closed" in cols,
        "has_src": "src" in cols,
    }
