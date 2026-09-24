"""kline.db schema contract (pa-data-source write → gate-signal-bot read-only)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

# Bump KLINE_SCHEMA.md together with this constant.
KLINE_SCHEMA_VERSION = 1

REQUIRED_COLUMNS = ("t", "symbol", "interval", "o", "h", "l", "c", "v", "sum", "ema20", "atr14")


class SchemaError(Exception):
    pass


def validate_kline_schema(db_path: Path) -> int:
    """Return schema version if the DB satisfies the v1 contract, else raise SchemaError."""
    path = Path(db_path)
    if not path.exists():
        raise SchemaError(f"kline db missing: {path}")
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(kline)").fetchall()
            }
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise SchemaError(f"kline db unreadable: {e}") from e
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise SchemaError(f"kline schema v{KLINE_SCHEMA_VERSION} missing columns: {missing} in {path}")
    return KLINE_SCHEMA_VERSION


def schema_ok(db_path: Optional[Path]) -> bool:
    if not db_path:
        return False
    try:
        validate_kline_schema(db_path)
        return True
    except SchemaError:
        return False
