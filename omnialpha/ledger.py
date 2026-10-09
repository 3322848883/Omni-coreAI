"""SQLite ledger for multi-bot audit (data/bots.db).

Tables: trades, plans, signals, heartbeats. WAL, single-file, thread-safe enough
for one writer per process.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  bot_id TEXT NOT NULL,
  plan_cycle TEXT,
  action TEXT,
  symbol TEXT,
  size_usd REAL,
  price REAL,
  order_ids TEXT,
  ok INTEGER,
  steps_json TEXT,
  source TEXT,
  symbols_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_bot_ts ON trades(bot_id, ts);

CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  bot_id TEXT NOT NULL,
  cycle_id TEXT,
  trigger TEXT,
  orders INTEGER,
  notes TEXT,
  reasoning TEXT,
  raw_json TEXT,
  symbols_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_bot_ts ON plans(bot_id, ts);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  bot_id TEXT NOT NULL,
  path TEXT,
  status TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_bot_ts ON signals(bot_id, ts);

CREATE TABLE IF NOT EXISTS heartbeats (
  bot_id TEXT NOT NULL,
  component TEXT NOT NULL,
  ts REAL NOT NULL,
  pid INTEGER,
  detail TEXT,
  PRIMARY KEY (bot_id, component)
);
"""


# 加列清单（T15）：给旧库补列用 —— `CREATE TABLE IF NOT EXISTS` 不会补已有表的列。
_ADDED_COLUMNS = (
    ("trades", "symbols_json", "TEXT"),
    ("plans", "symbols_json", "TEXT"),
)

# 按币过滤（T15）：`LIKE '%"BTC_USDT"%'` 不够 —— symbol 里带下划线，而 `_` 是 LIKE 的
# 单字符通配符，`"BTCXUSDT"` 也会被捞出来。用 `json_each` 做数组元素**精确**匹配。
_SYMBOL_FILTER = (
    " AND EXISTS (SELECT 1 FROM json_each(COALESCE({table}.symbols_json,'[]'))"
    " WHERE json_each.value=?)"
)


def symbols_from_steps(steps) -> list[str]:
    """从执行步骤里提取 symbol（**去重保序**）。

    `ExecReport.to_dict()` 把 symbol 放在**步骤顶层**（`{"action":…, "symbol":…}`）。
    取不到就返回空列表 —— **不猜**：历史 jsonl 与降级轮可能根本没有这个字段，
    编一个币写进审计库比留空更糟。
    """
    out: list[str] = []
    for s in (steps or []):
        if not isinstance(s, dict):
            continue
        v = str(s.get("symbol") or "").strip().upper()
        if v and v not in out:
            out.append(v)
    return out


class Ledger:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._add_missing_columns()
            self._conn.commit()

    def _add_missing_columns(self) -> None:
        """给**已存在**的旧表补列。

        `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作 —— 线上库是加
        `symbols_json` 之前建的，光靠 SCHEMA 永远补不上这一列（写入会报
        no such column）。所以显式 PRAGMA 查一遍再 ALTER，幂等可重入。
        """
        for table, col, typ in _ADDED_COLUMNS:
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    @staticmethod
    def _symbols_of(kw: dict) -> list[str]:
        """把 `symbols=[...]` / `symbol="A,B"` / `symbol="A"` 归一成**去重保序**的列表。

        单币时结果就是 `["A"]` —— 写进 `symbol` 列的值与改动前**逐字相同**。
        """
        raw = kw.get("symbols")
        if raw is None:
            raw = kw.get("symbol")
        if isinstance(raw, str):
            raw = [p for p in raw.replace(" ", "").split(",") if p]
        out: list[str] = []
        for s in (raw or []):
            s = str(s or "").strip().upper()
            if s and s not in out:
                out.append(s)
        return out

    def insert_trade(self, bot_id: str, **kw: Any) -> None:
        syms = self._symbols_of(kw)
        self._exec(
            "INSERT INTO trades(ts,bot_id,plan_cycle,action,symbol,size_usd,price,order_ids,ok,"
            "steps_json,source,symbols_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                kw.get("ts") or time.time(),
                bot_id,
                kw.get("plan_cycle"),
                kw.get("action"),
                # 单币 = 该币（与改动前一致）；多币 = 逗号串（便于人读）；取不到就留 NULL。
                ",".join(syms) if syms else None,
                kw.get("size_usd"),
                kw.get("price"),
                json.dumps(kw.get("order_ids") or [], ensure_ascii=False),
                1 if kw.get("ok") else 0,
                json.dumps(kw.get("steps") or kw.get("steps_json") or [], ensure_ascii=False),
                kw.get("source") or "run",
                json.dumps(syms, ensure_ascii=False) if syms else None,
            ),
        )

    def insert_plan(self, bot_id: str, **kw: Any) -> None:
        syms = self._symbols_of(kw)
        self._exec(
            "INSERT INTO plans(ts,bot_id,cycle_id,trigger,orders,notes,reasoning,raw_json,"
            "symbols_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                kw.get("ts") or time.time(),
                bot_id,
                kw.get("cycle_id"),
                kw.get("trigger"),
                kw.get("orders"),
                json.dumps(kw.get("notes") or [], ensure_ascii=False),
                kw.get("reasoning"),
                json.dumps(kw.get("raw") or kw.get("raw_json") or {}, ensure_ascii=False),
                json.dumps(syms, ensure_ascii=False) if syms else None,
            ),
        )

    def insert_signal(self, bot_id: str, path: str, status: str, error: str = "") -> None:
        self._exec(
            "INSERT INTO signals(ts,bot_id,path,status,error) VALUES(?,?,?,?,?)",
            (time.time(), bot_id, path, status, error or ""),
        )

    def heartbeat(self, bot_id: str, component: str, pid: Optional[int] = None, detail: str = "") -> None:
        self._exec(
            "INSERT INTO heartbeats(bot_id,component,ts,pid,detail) VALUES(?,?,?,?,?)"
            " ON CONFLICT(bot_id,component) DO UPDATE SET ts=excluded.ts,pid=excluded.pid,detail=excluded.detail",
            (bot_id, component, time.time(), pid, detail or ""),
        )

    def heartbeats(self, max_age_sec: float = 0) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM heartbeats").fetchall()
        now = time.time()
        out = []
        for r in rows:
            d = dict(r)
            d["age_s"] = max(0.0, now - float(d.get("ts") or 0))
            if max_age_sec and d["age_s"] > max_age_sec:
                continue
            out.append(d)
        return out

    def recent_trades(self, bot_id: str, limit: int = 20,
                      symbol: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE bot_id=?"
        params: list = [bot_id]
        if symbol:
            sql += _SYMBOL_FILTER.format(table="trades")
            params.append(str(symbol).strip().upper())
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def recent_plans(self, bot_id: str, limit: int = 20,
                     symbol: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM plans WHERE bot_id=?"
        params: list = [bot_id]
        if symbol:
            sql += _SYMBOL_FILTER.format(table="plans")
            params.append(str(symbol).strip().upper())
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def default_ledger_path(root: Path) -> Path:
    p = Path(root).resolve() / "data" / "bots.db"
    return p
