"""本地模拟盘账户库（paper_account.db）——复刻交易所账户/订单/持仓状态。

每 bot 一库：data/bots/<bot_id>/paper/account.db
八表：config / account / positions / orders / price_orders / fills / funding_log / pnl_snapshot
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# 订单状态机（对齐交易所）
ORDER_PLACED = "placed"
ORDER_OPEN = "open"
ORDER_PARTIALLY_FILLED = "partially_filled"
ORDER_FILLED = "filled"
ORDER_CANCELLED = "cancelled"
ORDER_REJECTED = "rejected"

# 触发单状态
TRIGGER_UNTRIGGERED = "untriggered"
TRIGGER_TRIGGERED = "triggered"
TRIGGER_FILLED = "filled"
TRIGGER_CANCELLED = "cancelled"
TRIGGER_REJECTED = "rejected"

# 委托有效期
TIF_GTC = "GTC"
TIF_IOC = "IOC"
TIF_FOK = "FOK"
TIF_PO = "PO"  # post only

DDL = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL DEFAULT 0,
    available REAL NOT NULL DEFAULT 0,
    position_margin REAL NOT NULL DEFAULT 0,
    order_margin REAL NOT NULL DEFAULT 0,
    unrealised_pnl REAL NOT NULL DEFAULT 0,
    leverage REAL NOT NULL DEFAULT 20,
    position_mode TEXT NOT NULL DEFAULT 'single',
    margin_mode TEXT NOT NULL DEFAULT 'isolated',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    contract TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'single',
    size REAL NOT NULL DEFAULT 0,
    entry_price REAL NOT NULL DEFAULT 0,
    leverage REAL NOT NULL DEFAULT 20,
    margin REAL NOT NULL DEFAULT 0,
    liquidation_price REAL,
    margin_mode TEXT NOT NULL DEFAULT 'isolated',
    realised_pnl REAL NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (contract, mode)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    contract TEXT NOT NULL,
    size REAL NOT NULL,
    price REAL,
    tif TEXT NOT NULL DEFAULT 'GTC',
    type TEXT NOT NULL DEFAULT 'limit',
    status TEXT NOT NULL,
    text TEXT DEFAULT '',
    filled_size REAL NOT NULL DEFAULT 0,
    avg_price REAL,
    reduce_only INTEGER NOT NULL DEFAULT 0,
    create_time INTEGER NOT NULL,
    finish_time INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS price_orders (
    order_id TEXT PRIMARY KEY,
    contract TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    trigger_price_type TEXT NOT NULL DEFAULT 'latest',
    order_type TEXT NOT NULL DEFAULT 'limit',
    price REAL,
    size REAL NOT NULL,
    side TEXT NOT NULL,
    rule INTEGER NOT NULL DEFAULT 0,
    reduce_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    text TEXT DEFAULT '',
    create_time INTEGER NOT NULL,
    trigger_time INTEGER,
    finish_time INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_time INTEGER NOT NULL,
    contract TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    realised_pnl REAL NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'taker',
    order_id TEXT,
    kind TEXT NOT NULL DEFAULT 'trade'
);
CREATE TABLE IF NOT EXISTS funding_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settle_time INTEGER NOT NULL,
    contract TEXT NOT NULL,
    rate REAL NOT NULL,
    amount REAL NOT NULL,
    position_side TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pnl_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_time INTEGER NOT NULL,
    equity REAL NOT NULL,
    unrealised REAL NOT NULL,
    realised REAL NOT NULL,
    drawdown REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_contract ON orders (contract, status);
CREATE INDEX IF NOT EXISTS idx_price_orders_contract ON price_orders (contract, status);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills (fill_time);
CREATE INDEX IF NOT EXISTS idx_funding_time ON funding_log (settle_time);
"""

DEFAULT_CONFIG = {
    "initial_capital": "10000",
    "leverage": "20",
    "fee_rate": "0.0005",
    "maker_fee_rate": "0.0",
    "funding_enabled": "1",
    "position_mode": "single",
    "margin_mode": "isolated",
    "price_band_pct": "5.0",
    "maintenance_margin_rate": "0.005",
    "trigger_price_type": "latest",
    "feed_exchange": "gate",
}


def new_order_id() -> str:
    return uuid.uuid4().hex[:16]


class PaperStore:
    """paper_account.db 的线程安全读写层。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    def _db(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(DDL)
                conn.commit()
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ── config ──────────────────────────────────────────
    def init_config(self, overrides: Optional[dict] = None) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        if overrides:
            for k, v in overrides.items():
                if v is not None:
                    cfg[str(k)] = str(v)
        conn = self._db()
        with self._lock:
            for k, v in cfg.items():
                conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?,?)", (k, v))
            # 首次开户写入初始资金
            row = conn.execute("SELECT id FROM account WHERE id=1").fetchone()
            if row is None:
                cap = float(cfg.get("initial_capital") or 10000)
                now = int(time.time())
                conn.execute(
                    "INSERT INTO account (id, balance, available, leverage, position_mode, margin_mode, updated_at)"
                    " VALUES (1,?,?,?,?,?,?)",
                    (cap, cap, float(cfg.get("leverage") or 20),
                     cfg.get("position_mode") or "single",
                     cfg.get("margin_mode") or "isolated", now),
                )
            conn.commit()
        return cfg

    def get_config(self) -> dict:
        conn = self._db()
        with self._lock:
            rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def cfg_f(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get_config().get(key) or default)
        except (TypeError, ValueError):
            return default

    def cfg_s(self, key: str, default: str = "") -> str:
        return str(self.get_config().get(key) or default)

    # ── account ─────────────────────────────────────────
    def get_account(self) -> dict:
        conn = self._db()
        with self._lock:
            row = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
        return dict(row) if row else {}

    def update_account(self, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        sets = ", ".join(f"{k}=?" for k in fields)
        conn = self._db()
        with self._lock:
            conn.execute(f"UPDATE account SET {sets} WHERE id=1", tuple(fields.values()))
            conn.commit()

    # ── positions ───────────────────────────────────────
    def get_positions(self, contract: Optional[str] = None) -> list[dict]:
        conn = self._db()
        with self._lock:
            if contract:
                rows = conn.execute("SELECT * FROM positions WHERE contract=?", (contract,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM positions").fetchall()
        return [dict(r) for r in rows if r["size"] != 0]

    def upsert_position(self, contract: str, mode: str, **fields: Any) -> None:
        fields["updated_at"] = int(time.time())
        keys = ["contract", "mode"] + list(fields)
        conn = self._db()
        with self._lock:
            conn.execute(
                f"INSERT OR REPLACE INTO positions ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                (contract, mode) + tuple(fields.values()),
            )
            conn.commit()

    def delete_position(self, contract: str, mode: str = "single") -> None:
        conn = self._db()
        with self._lock:
            conn.execute("DELETE FROM positions WHERE contract=? AND mode=?", (contract, mode))
            conn.commit()

    # ── orders ──────────────────────────────────────────
    def insert_order(self, order: dict) -> None:
        cols = ["order_id", "contract", "size", "price", "tif", "type", "status",
                "text", "filled_size", "avg_price", "reduce_only", "create_time",
                "finish_time", "error"]
        vals = [order.get(c) for c in cols]
        if not vals[0]:
            vals[0] = new_order_id()
        if vals[11] is None:
            vals[11] = int(time.time())
        vals[8] = vals[8] or 0
        vals[10] = 1 if vals[10] else 0
        conn = self._db()
        with self._lock:
            conn.execute(
                f"INSERT INTO orders ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(vals),
            )
            conn.commit()

    def update_order(self, order_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn = self._db()
        with self._lock:
            conn.execute(f"UPDATE orders SET {sets} WHERE order_id=?", tuple(fields.values()) + (order_id,))
            conn.commit()

    def get_order(self, order_id: str) -> Optional[dict]:
        conn = self._db()
        with self._lock:
            row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def list_orders(self, contract: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        q, p = "SELECT * FROM orders WHERE 1=1", []
        if contract:
            q += " AND contract=?"
            p.append(contract)
        if status:
            q += " AND status=?"
            p.append(status)
        q += " ORDER BY create_time DESC"
        conn = self._db()
        with self._lock:
            rows = conn.execute(q, tuple(p)).fetchall()
        return [dict(r) for r in rows]

    # ── price orders（触发单）────────────────────────────
    def insert_price_order(self, po: dict) -> None:
        cols = ["order_id", "contract", "trigger_price", "trigger_price_type",
                "order_type", "price", "size", "side", "rule", "reduce_only", "status",
                "text", "create_time", "trigger_time", "finish_time", "error"]
        vals = [po.get(c) for c in cols]
        if not vals[0]:
            vals[0] = new_order_id()
        if vals[12] is None:
            vals[12] = int(time.time())
        vals[8] = int(vals[8] or 0)
        vals[9] = 1 if vals[9] else 0
        conn = self._db()
        with self._lock:
            conn.execute(
                f"INSERT INTO price_orders ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(vals),
            )
            conn.commit()

    def update_price_order(self, order_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn = self._db()
        with self._lock:
            conn.execute(f"UPDATE price_orders SET {sets} WHERE order_id=?",
                         tuple(fields.values()) + (order_id,))
            conn.commit()

    def get_price_order(self, order_id: str) -> Optional[dict]:
        conn = self._db()
        with self._lock:
            row = conn.execute("SELECT * FROM price_orders WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def list_price_orders(self, contract: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        q, p = "SELECT * FROM price_orders WHERE 1=1", []
        if contract:
            q += " AND contract=?"
            p.append(contract)
        if status:
            q += " AND status=?"
            p.append(status)
        q += " ORDER BY create_time DESC"
        conn = self._db()
        with self._lock:
            rows = conn.execute(q, tuple(p)).fetchall()
        return [dict(r) for r in rows]

    # ── fills ───────────────────────────────────────────
    def insert_fill(self, fill: dict) -> int:
        conn = self._db()
        with self._lock:
            cur = conn.execute(
                "INSERT INTO fills (fill_time, contract, side, price, size, fee,"
                " realised_pnl, role, order_id, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    int(fill.get("fill_time") or time.time()),
                    fill["contract"], fill["side"],
                    float(fill["price"]), float(fill["size"]),
                    float(fill.get("fee") or 0), float(fill.get("realised_pnl") or 0),
                    fill.get("role") or "taker", fill.get("order_id"),
                    fill.get("kind") or "trade",
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_fills(self, contract: Optional[str] = None, limit: int = 100) -> list[dict]:
        conn = self._db()
        with self._lock:
            if contract:
                rows = conn.execute(
                    "SELECT * FROM fills WHERE contract=? ORDER BY fill_time DESC LIMIT ?",
                    (contract, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fills ORDER BY fill_time DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # ── funding ─────────────────────────────────────────
    def insert_funding(self, rec: dict) -> None:
        conn = self._db()
        with self._lock:
            conn.execute(
                "INSERT INTO funding_log (settle_time, contract, rate, amount, position_side)"
                " VALUES (?,?,?,?,?)",
                (
                    int(rec.get("settle_time") or time.time()),
                    rec["contract"], float(rec["rate"]),
                    float(rec["amount"]), rec.get("position_side") or "",
                ),
            )
            conn.commit()

    def last_funding_time(self, contract: Optional[str] = None) -> int:
        conn = self._db()
        with self._lock:
            if contract:
                row = conn.execute(
                    "SELECT MAX(settle_time) FROM funding_log WHERE contract=?", (contract,)
                ).fetchone()
            else:
                row = conn.execute("SELECT MAX(settle_time) FROM funding_log").fetchone()
        return int(row[0] or 0) if row else 0

    # ── pnl snapshot ────────────────────────────────────
    def insert_pnl(self, snap: dict) -> None:
        conn = self._db()
        with self._lock:
            conn.execute(
                "INSERT INTO pnl_snapshot (snap_time, equity, unrealised, realised, drawdown)"
                " VALUES (?,?,?,?,?)",
                (
                    int(snap.get("snap_time") or time.time()),
                    float(snap.get("equity") or 0), float(snap.get("unrealised") or 0),
                    float(snap.get("realised") or 0), float(snap.get("drawdown") or 0),
                ),
            )
            conn.commit()

    def last_pnl(self) -> Optional[dict]:
        conn = self._db()
        with self._lock:
            row = conn.execute("SELECT * FROM pnl_snapshot ORDER BY snap_time DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def total_realised(self) -> float:
        conn = self._db()
        with self._lock:
            row = conn.execute("SELECT COALESCE(SUM(realised_pnl),0) FROM fills").fetchone()
        return float(row[0] or 0) if row else 0.0

    def total_fees(self) -> float:
        conn = self._db()
        with self._lock:
            row = conn.execute("SELECT COALESCE(SUM(fee),0) FROM fills").fetchone()
        return float(row[0] or 0) if row else 0.0

    def total_funding(self) -> float:
        conn = self._db()
        with self._lock:
            row = conn.execute("SELECT COALESCE(SUM(amount),0) FROM funding_log").fetchone()
        return float(row[0] or 0) if row else 0.0
