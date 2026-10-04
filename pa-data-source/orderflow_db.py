"""订单流落库 —— 独立库 `orderflow.db`，与 kline.db 分开避免影响现有接缝。

四张表对应聚合层的四种输出：
  of_book_state  盘口状态（5s）—— 价差/深度/撤单率/成交密度 + 分级
  of_tape        逐笔聚合（1s）—— 主动买卖量/Delta/大单
  of_footprint   逐价位成交（1m × 价位）—— 真 footprint
  of_walls       长寿挂单（事件型）—— 吸收判据的输入

保留期与 fetch_aux 的 TS_TTL 同一风格；清理复用 DELETE-by-cutoff。
"""
from __future__ import annotations

import os
import sqlite3
import time

SCHEMA_VERSION = 1

# 表 -> 保留期（秒）
TS_TTL = {
    "of_book_state": 30 * 86400,
    "of_tape": 7 * 86400,
    "of_footprint": 30 * 86400,
    "of_walls": 7 * 86400,
}

_DDL = (
    """CREATE TABLE IF NOT EXISTS of_book_state (
        ts INTEGER NOT NULL,
        contract TEXT NOT NULL,
        spread_pct REAL,
        depth_bid REAL,
        depth_ask REAL,
        depth_ratio REAL,
        place_vol REAL,
        cancel_vol REAL,
        cancel_rate REAL,
        traded_vol REAL,
        intensity REAL,
        grade TEXT,
        levels INTEGER,
        PRIMARY KEY (ts, contract)
    )""",
    """CREATE TABLE IF NOT EXISTS of_tape (
        ts INTEGER NOT NULL,
        contract TEXT NOT NULL,
        buy_size REAL,
        sell_size REAL,
        delta REAL,
        big_count INTEGER,
        big_size REAL,
        max_trade REAL,
        PRIMARY KEY (ts, contract)
    )""",
    """CREATE TABLE IF NOT EXISTS of_footprint (
        ts INTEGER NOT NULL,
        contract TEXT NOT NULL,
        price REAL NOT NULL,
        buy_size REAL,
        sell_size REAL,
        PRIMARY KEY (ts, contract, price)
    )""",
    """CREATE TABLE IF NOT EXISTS of_walls (
        ts INTEGER NOT NULL,
        contract TEXT NOT NULL,
        side TEXT,
        price REAL,
        peak_size REAL,
        age_sec REAL,
        outcome TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_of_walls_ts ON of_walls (ts)",
    "CREATE INDEX IF NOT EXISTS idx_of_tape_ts ON of_tape (ts)",
)


def connect(db_path: str) -> sqlite3.Connection:
    d = os.path.dirname(os.path.abspath(db_path))
    if d:
        os.makedirs(d, exist_ok=True)
    # check_same_thread=False：连接在采集器构造时建立（主线程），而 flush 跑在
    # 后台线程。调用方必须用锁串行化访问——见 OrderFlowCollector._lock。
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in _DDL:
        conn.execute(stmt)
    conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    conn.commit()
    return conn


def write_book_state(conn: sqlite3.Connection, ts: int, contract: str, m: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO of_book_state "
        "(ts,contract,spread_pct,depth_bid,depth_ask,depth_ratio,place_vol,"
        " cancel_vol,cancel_rate,traded_vol,intensity,grade,levels) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, contract, m.get("spread_pct"), m.get("depth_bid"), m.get("depth_ask"),
         m.get("depth_ratio"), m.get("place_vol"), m.get("cancel_vol"),
         m.get("cancel_rate"), m.get("traded_vol"), m.get("intensity"),
         m.get("grade"), m.get("levels")),
    )


def write_tape(conn: sqlite3.Connection, ts: int, contract: str, b: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO of_tape "
        "(ts,contract,buy_size,sell_size,delta,big_count,big_size,max_trade) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts, contract, b.get("buy_size"), b.get("sell_size"), b.get("delta"),
         b.get("big_count"), b.get("big_size"), b.get("max_trade")),
    )


def write_footprint(conn: sqlite3.Connection, ts: int, contract: str,
                    fp: dict) -> None:
    if not fp:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO of_footprint (ts,contract,price,buy_size,sell_size) "
        "VALUES (?,?,?,?,?)",
        [(ts, contract, float(p), v[0], v[1]) for p, v in fp.items()],
    )


def write_walls(conn: sqlite3.Connection, ts: int, contract: str,
                walls: list) -> None:
    if not walls:
        return
    conn.executemany(
        "INSERT INTO of_walls (ts,contract,side,price,peak_size,age_sec,outcome) "
        "VALUES (?,?,?,?,?,?,?)",
        [(ts, contract, w.get("side"), w.get("price"), w.get("peak_size"),
          w.get("age_sec"), w.get("outcome")) for w in walls],
    )


def purge(conn: sqlite3.Connection, now: int | None = None) -> int:
    """按 TTL 清理过期行，返回删除行数。"""
    now = int(now or time.time())
    removed = 0
    for table, ttl in TS_TTL.items():
        cutoff = now - ttl
        cur = conn.execute("DELETE FROM %s WHERE ts < ?" % table, (cutoff,))
        removed += cur.rowcount or 0
    conn.commit()
    return removed


def latest_ts(conn: sqlite3.Connection, table: str, contract: str) -> int | None:
    try:
        row = conn.execute(
            "SELECT MAX(ts) FROM %s WHERE contract=?" % table, (contract,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if not row or row[0] is None else int(row[0])
