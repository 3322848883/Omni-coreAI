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
  source TEXT
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
  raw_json TEXT
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
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def insert_trade(self, bot_id: str, **kw: Any) -> None:
        self._exec(
            "INSERT INTO trades(ts,bot_id,plan_cycle,action,symbol,size_usd,price,order_ids,ok,steps_json,source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                kw.get("ts") or time.time(),
                bot_id,
                kw.get("plan_cycle"),
                kw.get("action"),
                kw.get("symbol"),
                kw.get("size_usd"),
                kw.get("price"),
                json.dumps(kw.get("order_ids") or [], ensure_ascii=False),
                1 if kw.get("ok") else 0,
                json.dumps(kw.get("steps") or kw.get("steps_json") or [], ensure_ascii=False),
                kw.get("source") or "run",
            ),
        )

    def insert_plan(self, bot_id: str, **kw: Any) -> None:
        self._exec(
            "INSERT INTO plans(ts,bot_id,cycle_id,trigger,orders,notes,reasoning,raw_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                kw.get("ts") or time.time(),
                bot_id,
                kw.get("cycle_id"),
                kw.get("trigger"),
                kw.get("orders"),
                json.dumps(kw.get("notes") or [], ensure_ascii=False),
                kw.get("reasoning"),
                json.dumps(kw.get("raw") or kw.get("raw_json") or {}, ensure_ascii=False),
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

    def recent_trades(self, bot_id: str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE bot_id=? ORDER BY ts DESC LIMIT ?", (bot_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_plans(self, bot_id: str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM plans WHERE bot_id=? ORDER BY ts DESC LIMIT ?", (bot_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def default_ledger_path(root: Path) -> Path:
    p = Path(root).resolve() / "data" / "bots.db"
    return p
