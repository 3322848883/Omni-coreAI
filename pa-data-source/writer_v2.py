"""v2 写入器：UPSERT 语义 + closed 判定 + 未收线节流 + 指标增量。

为什么必须换掉旧的写路径
1. 旧 `upsert_candle()` 用 ``INSERT OR REPLACE`` —— REPLACE 是「删+插」，会把该行**所有指标列清成 NULL**
   （v1 之所以没炸是因为写完立刻重算两列），并且产生 2 倍 B 树写。
   v2 起指标列有 11 个，必须改 ``ON CONFLICT DO UPDATE`` 只更新 OHLCV/closed/src。
2. 旧路径每根 bar 都跑一次 `retention_cleanup()`（COUNT + DELETE），1m 周期 30 次/分；改为**每 N 次或每 5 分钟**。
3. 旧路径把 Gate `w` 字段（是否收线）丢掉，未收线 bar 也写指标 → 指标被半根 K 线污染。

本模块只做「纯逻辑 + 参数化连接」，不 import watcher，方便单测。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import indicators_core as core

__all__ = [
    "Bar",
    "SeriesWriter",
    "WriteResult",
    "upsert_bar",
    "should_write_unclosed",
    "interval_seconds",
    "INTERVAL_SECONDS",
    "SRC_WS",
    "SRC_REST",
    "SRC_BACKFILL",
    "SRC_SYNTH",
    "SRC_MIGRATED",
]

SRC_WS, SRC_REST, SRC_BACKFILL, SRC_SYNTH, SRC_MIGRATED = (
    "ws", "rest", "backfill", "synthetic", "migrated")

INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "7d": 604800, "30d": 2592000,
}


def interval_seconds(interval: str) -> int:
    return INTERVAL_SECONDS.get(str(interval).lower(), 900)


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0
    sum: float = 0.0
    closed: bool = True
    src: str = SRC_WS

    def validate(self) -> None:
        if not (1_600_000_000 < int(self.t) < 100_000_000_000):
            raise ValueError(f"t 必须是 Unix 秒（1.6e9~1e11），got {self.t!r}")
        if self.h < max(self.o, self.c) or self.l > min(self.o, self.c):
            raise ValueError(f"OHLC 非法: {self.o},{self.h},{self.l},{self.c}")


@dataclass
class WriteResult:
    written: bool
    closed: bool
    skipped_reason: str = ""
    values: dict = field(default_factory=dict)


def table_columns(conn: sqlite3.Connection, table: str = "kline") -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()}


def upsert_bar(conn: sqlite3.Connection, symbol: str, interval: str, bar: Bar,
               indicator_cols: Iterable[str] = (), values: Optional[dict] = None,
               present: Optional[set] = None) -> int:
    """写一根 bar，指标列单独 UPDATE（避免 REPLACE 清列）。

    **列感知**：v1 表没有 `closed`/`src` 列 → 自动省略，同一个函数既能写 v1 也能写 v2。
    `present` 可由调用方缓存（每根都 PRAGMA 一次没必要）。
    """
    bar.validate()
    have = present if present is not None else table_columns(conn)
    base = ["symbol", "interval", "t", "o", "h", "l", "c", "v", "sum"]
    extra = [c for c in ("closed", "src") if c in have]
    cols = base + extra
    args = [symbol, interval, int(bar.t), bar.o, bar.h, bar.l, bar.c, bar.v, bar.sum]
    if "closed" in have:
        args.append(1 if bar.closed else 0)
    if "src" in have:
        args.append(bar.src)
    updates = "o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v, sum=excluded.sum"
    if "closed" in have:
        updates += ", closed=excluded.closed"
    if "src" in have:
        updates += ", src=excluded.src"
    sql = (f"INSERT INTO kline ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT(symbol, interval, t) DO UPDATE SET {updates}")
    cur = conn.execute(sql, args)
    if values:
        sets, vals = [], []
        for col in (indicator_cols or values.keys()):
            if col in values and col in have:
                sets.append(f"[{col}]=?")
                vals.append(values[col])
        if sets:
            conn.execute(
                f"UPDATE kline SET {', '.join(sets)} WHERE symbol=? AND interval=? AND t=?",
                (*vals, symbol, interval, int(bar.t)),
            )
    return int(cur.rowcount or 0)


def should_write_unclosed(interval: str, now: float,
                          last_write: Optional[float]) -> bool:
    """未收线 bar 的写节流：`clamp(interval/60, 5s, 300s)`。

    1m → 5s（12 次/分，原 30 次/分）；15m → 15s；4h/1d → 300s。
    """
    gap = max(5.0, min(300.0, interval_seconds(interval) / 60.0))
    return last_write is None or (now - last_write) >= gap


class SeriesWriter:
    """单序列写状态：StreamState + 节流时间戳 + 保留计数器。

    watcher 里每个 (symbol, interval) 复用一个实例；重连/重启时用 ``seed_from_db()``
    预热状态，避免首根指标从 SMA 重播种。
    """

    def __init__(self, specs, retention_every: int = 100, retention_sec: float = 300.0) -> None:
        self.specs = list(specs)
        self.cols = [c for s in self.specs for c in s.columns]
        self.state = core.StreamState([s.as_dict() for s in self.specs])
        self.last_unclosed_write: Optional[float] = None
        self._writes = 0
        self._last_retention = 0.0
        self.retention_every = retention_every
        self.retention_sec = retention_sec
        self._cols_cache: Optional[set] = None

    def compute(self, bar: Bar) -> dict:
        """只推进状态、返回指标值（不写库）。shadow 模式用它算「新引擎值」。"""
        return self.state.push(bar.o, bar.h, bar.l, bar.c)

    # ── 状态预热 ──────────────────────────────────────────────
    def seed_from_db(self, conn: sqlite3.Connection, symbol: str, interval: str,
                     warmup: int) -> int:
        """用库里最近 warmup 根把 StreamState 推到位；返回实际预热根数。"""
        rows = conn.execute(
            "SELECT o,h,l,c FROM kline WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?",
            (symbol, interval, int(warmup)),
        ).fetchall()
        for o, h, l, c in reversed(rows):
            self.state.push(float(o), float(h), float(l), float(c))
        return len(rows)

    # ── 写一根 ────────────────────────────────────────────────
    def columns(self, conn: sqlite3.Connection) -> set:
        if self._cols_cache is None:
            self._cols_cache = table_columns(conn)
        return self._cols_cache

    def write(self, conn: sqlite3.Connection, symbol: str, interval: str, bar: Bar,
              now: Optional[float] = None) -> WriteResult:
        now = time.time() if now is None else now
        if not bar.closed:
            if not should_write_unclosed(interval, now, self.last_unclosed_write):
                return WriteResult(False, False, "throttled")
            self.last_unclosed_write = now
            # 未收线：只写 OHLCV，不推指标状态（否则状态会被半根 bar 污染）
            upsert_bar(conn, symbol, interval, bar, present=self.columns(conn))
            self._writes += 1
            return WriteResult(True, False)

        vals = self.compute(bar)
        upsert_bar(conn, symbol, interval, bar, indicator_cols=self.cols, values=vals,
                   present=self.columns(conn))
        self._writes += 1
        return WriteResult(True, True, values=vals)

    # ── 保留触发 ──────────────────────────────────────────────
    def due_retention(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if self._writes and self._writes % max(1, self.retention_every) == 0:
            self._last_retention = now
            return True
        if now - self._last_retention >= self.retention_sec:
            self._last_retention = now
            return True
        return False
