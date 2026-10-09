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
    drawdown REAL NOT NULL DEFAULT 0,
    contract TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_contract ON orders (contract, status);
CREATE INDEX IF NOT EXISTS idx_price_orders_contract ON price_orders (contract, status);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills (fill_time);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills (order_id);
CREATE INDEX IF NOT EXISTS idx_funding_time ON funding_log (settle_time);
"""

# 加列清单（旧库补列用，见 `_add_missing_columns`）。
_ADDED_COLUMNS = (
    ("pnl_snapshot", "contract", "TEXT"),
)


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


def _pnl_agg(vals: list) -> dict:
    """一组已实现盈亏 → 计数/盈利笔数/合计/最差单笔。"""
    return {
        "trades": len(vals),
        "wins": sum(1 for v in vals if v > 0),
        "pnl": round(sum(vals), 2),
        "worst": round(min(vals), 2) if vals else 0.0,
    }


def realized_pnl_stats(db_path: Any, *, contract: Optional[str] = None,
                       by_contract: bool = False) -> Optional[dict]:
    """汇总 paper 账本的**已实现盈亏**（权威口径）：笔数 / 盈利笔数 / 合计 / 最差单笔。

    `fills.realised_pnl != 0` 即一笔平仓成交 —— **它包含交易所侧触发的平仓**
    （SL/TP），而那类没有信号、不进 `logs/trades.jsonl`。所以「这个 bot 到底平了
    几笔、赚亏多少」只能从这里取，不能从成交日志推。

    - `contract=`     只统计该合约（单币读法）
    - `by_contract=`  额外返回 `by_contract: {合约: 同一组计数}` —— 多币下**合并值
      回答不了「这个币赚没赚」**（一币亏一币赚会互相抵消）

    只读打开；库不存在 / 读不出 / 没有任何平仓 → 返回 None（调用方退回非账本口径）。
    """
    p = Path(db_path)
    if not p.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            # 兼容没有 `contract` 列的库（老库 / 测试手工建的最小表）：缺列时按
            # 「无币」读 —— 核心计数照常给，只是没有按币那一层。**不能因此整段读
            # 失败**（那会让画像静默退回文件累加值，正是这条投影要修掉的毛病）。
            have = {r[1] for r in con.execute("PRAGMA table_info(fills)")}
            col = "contract" if "contract" in have else "null"
            sql = f"select {col}, realised_pnl from fills where realised_pnl != 0"
            params: tuple = ()
            if contract and "contract" in have:
                sql += " and contract=?"
                params = (str(contract).strip().upper(),)
            pairs = [(r[0], float(r[1])) for r in con.execute(sql, params)]
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None
    if not pairs:
        return None
    rec = _pnl_agg([v for _, v in pairs])
    if contract:
        rec["contract"] = str(contract).strip().upper()
    if by_contract:
        groups: dict[str, list] = {}
        for c, v in pairs:
            key = str(c or "").strip().upper()
            if key:
                groups.setdefault(key, []).append(v)
        rec["by_contract"] = {c: _pnl_agg(vs) for c, vs in sorted(groups.items())}
    return rec


class _FileLock:
    """跨进程文件锁：防止两个进程同时写 account.db。"""

    def __init__(self, path: Path):
        self.path = Path(str(path) + ".lock")
        self._fh = None

    def acquire(self) -> None:
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001
            # 锁失败不阻塞（单机开发可容忍），但尽量释放
            pass

    def release(self) -> None:
        try:
            if self._fh is not None:
                import os
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()
        return False


class PaperStore:
    """paper_account.db 的线程安全读写层。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._flock = _FileLock(self.path)
        self._conn: Optional[sqlite3.Connection] = None

    def _write_guard(self):
        """线程锁 + 跨进程文件锁（写路径）。"""
        self._lock.acquire()
        self._flock.acquire()
        return self

    def _write_release(self) -> None:
        try:
            self._flock.release()
        finally:
            self._lock.release()

    def _db(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(DDL)
                self._add_missing_columns(conn)
                conn.commit()
                self._conn = conn
            return self._conn

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """给**已存在**的旧表补列 —— `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，
        线上 paper 库（加 `contract` 之前建的）光靠 DDL 永远补不上，写入会 no such column。
        幂等：PRAGMA 查一遍再 ALTER，重复打开不报错。
        """
        for table, col, typ in _ADDED_COLUMNS:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

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

    def set_config(self, key: str, value: Any) -> None:
        """写一条配置（UPSERT）。用于**逐合约**的持久化设置（如 `leverage:BTC_USDT`）。"""
        conn = self._db()
        with self._lock:
            with self._flock:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(key), str(value)),
                )
                conn.commit()

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
            with self._flock:
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
            with self._flock:
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
            with self._flock:
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
            with self._flock:
                conn.execute(f"UPDATE price_orders SET {sets} WHERE order_id=?",
                             tuple(fields.values()) + (order_id,))
                conn.commit()

    def claim_price_order(self, order_id: str) -> bool:
        """原子占用触发单：仅当 status=untriggered 时改为 triggered，防双触发。"""
        conn = self._db()
        with self._lock:
            with self._flock:
                cur = conn.execute(
                    "UPDATE price_orders SET status=?, trigger_time=? "
                    "WHERE order_id=? AND status='untriggered'",
                    (TRIGGER_TRIGGERED, int(time.time()), order_id),
                )
                conn.commit()
                return cur.rowcount == 1

    def cancel_reduce_only_price_orders(self, contract: str, keep_ids: Optional[set] = None,
                                        mode: Optional[str] = None) -> list[str]:
        """仓位归零后回收孤儿保护单（仅 reduce_only 且未触发）。

        `mode` 用于**双向模式**：同一个 contract 上 long 与 short 各有自己的保护单，
        平掉一侧时**只能撤那一侧的** —— 否则平多单会把空单的 SL/TP 一并撤掉，
        剩下一侧裸奔（审计 C-16）。保护单归哪一侧由 `side` 定：`sell` 平多、`buy` 平空。
        `mode=None`（单仓模式）保持原行为：撤该合约上全部未触发的 reduce_only 单。
        """
        keep = {str(x) for x in (keep_ids or set())}
        sql = ("SELECT order_id FROM price_orders WHERE contract=? AND reduce_only=1 "
               "AND status IN ('untriggered','open','triggered')")
        params: list = [contract]
        if mode and ("long" in str(mode) or "short" in str(mode)):
            # 只有**真双向**仓（mode 是 long/short/dual_long/dual_short）才需要分侧；
            # `single` 是单仓模式的默认值，必须保持「撤该合约全部」的原行为。
            sql += " AND side=?"
            params.append("sell" if "long" in str(mode) else "buy")
        conn = self._db()
        cancelled: list[str] = []
        with self._lock:
            with self._flock:
                rows = conn.execute(sql, tuple(params)).fetchall()
                for r in rows:
                    oid = str(r["order_id"])
                    if oid in keep:
                        continue
                    conn.execute(
                        "UPDATE price_orders SET status=?, finish_time=?, error='orphan_cleanup' "
                        "WHERE order_id=? AND status IN ('untriggered','open','triggered')",
                        (TRIGGER_CANCELLED, int(time.time()), oid),
                    )
                    cancelled.append(oid)
                conn.commit()
        return cancelled

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
    def insert_fill(self, fill: dict) -> tuple[int, bool]:
        """写入成交；返回 (id, created)。created=False 表示重复入账已忽略。"""
        conn = self._db()
        with self._lock:
            with self._flock:
                # 幂等：同 order 同价同量同秒 只入账一次（防并发双花）
                if fill.get("order_id"):
                    row = conn.execute(
                        "SELECT id FROM fills WHERE order_id=? AND fill_time=? AND price=? AND size=? LIMIT 1",
                        (fill.get("order_id"), fill.get("fill_time"), fill.get("price"), fill.get("size")),
                    ).fetchone()
                    if row:
                        return int(row["id"]), False
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
                return int(cur.lastrowid or 0), True

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
        """落一条盈亏快照。`contract=None` = 账户级；给了就是**该币**的快照。

        为什么要有按币快照：只存账户级合并值时，「这个币到底赚没赚」在多币下
        永远答不出来（合并值一个币亏另一个币赚会互相抵消）。
        """
        conn = self._db()
        with self._lock:
            conn.execute(
                "INSERT INTO pnl_snapshot (snap_time, equity, unrealised, realised, drawdown,"
                " contract) VALUES (?,?,?,?,?,?)",
                (
                    int(snap.get("snap_time") or time.time()),
                    float(snap.get("equity") or 0), float(snap.get("unrealised") or 0),
                    float(snap.get("realised") or 0), float(snap.get("drawdown") or 0),
                    (str(snap.get("contract") or "").strip().upper() or None),
                ),
            )
            conn.commit()

    def last_pnl(self, contract: Optional[str] = None) -> Optional[dict]:
        """最近一条快照。`contract=None` 取**账户级**（`contract IS NULL`）那条 ——
        不是「最新的一条」，否则按币快照会顶掉账户级读数。
        """
        conn = self._db()
        sql = "SELECT * FROM pnl_snapshot"
        params: tuple = ()
        if contract:
            sql += " WHERE contract=?"
            params = (str(contract).strip().upper(),)
        else:
            sql += " WHERE contract IS NULL"
        sql += " ORDER BY snap_time DESC LIMIT 1"
        with self._lock:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def pnl_by_contract(self) -> dict[str, dict]:
        """每个币最近一条快照 → `{contract: row}`（按币盈亏的读取面）。"""
        conn = self._db()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM pnl_snapshot WHERE contract IS NOT NULL"
                " ORDER BY snap_time DESC"
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            c = str(r["contract"])
            if c not in out:
                out[c] = dict(r)
        return out

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
