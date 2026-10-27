"""写入引擎选择层：legacy / shadow / v2（watcher 只调这里）。

三种模式
- ``legacy``：完全保持旧行为（`compute_latest_ema/atr` 的单步递推 + 每根写两列）。
- ``shadow``：**算两套、写旧值**。旧值入库（下游零影响），新引擎只比对并累积
  ``ShadowStats`` —— 用于切换前 24h 观察失配率（门禁 <0.1%）。
- ``v2``：用 `SeriesWriter`（StreamState 增量 + UPSERT），写配置决定的全部指标列。

开关（显式参数 > 环境变量）
    PA_INDICATOR_ENGINE=legacy|shadow|v2
    PA_KLINE_V2=1              → 等价 v2
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
import sqlite3
from typing import Iterable, Optional

import indicators_core as core  # noqa: F401  （供调用方复用同一实现）
from shadow import ShadowStats
from writer_v2 import SRC_WS, Bar, SeriesWriter, table_columns, upsert_bar

__all__ = ["ENGINES", "resolve_engine", "legacy_values", "SeriesRegistry"]

ENGINES = ("legacy", "shadow", "v2")

_EMA_K = 2.0 / 21.0
_ATR_PERIOD = 14


def resolve_engine(explicit: Optional[str] = None) -> str:
    if explicit:
        e = str(explicit).strip().lower()
        if e not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}, got {explicit!r}")
        return e
    env = (os.environ.get("PA_INDICATOR_ENGINE") or "").strip().lower()
    if env in ENGINES:
        return env
    if (os.environ.get("PA_KLINE_V2") or "").strip() in ("1", "true", "yes", "on"):
        return "v2"
    return "legacy"


def _round8(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 8)


def legacy_values(conn: sqlite3.Connection, symbol: str, interval: str, bar: Bar) -> dict:
    """复刻旧实现（`compute_latest_ema` / `compute_latest_atr`）的单步递推。

    与旧代码一致的三点：只看 `t < 本根` 且指标非 NULL 的上一根、EMA k=2/21（固定 20 周期）、
    ATR 用 `(prev*13 + tr)/14`，结果 `round(..., 8)`（旧实现写的是 8 位小数字符串）。
    """
    out: dict = {"ema20": None, "atr14": None}
    row = conn.execute(
        "SELECT ema20 FROM kline WHERE symbol=? AND interval=? AND ema20 IS NOT NULL "
        "AND t < ? ORDER BY t DESC LIMIT 1", (symbol, interval, int(bar.t))).fetchone()
    if row is not None:
        out["ema20"] = _round8(float(bar.c) * _EMA_K + float(row[0]) * (1 - _EMA_K))

    row = conn.execute(
        "SELECT atr14 FROM kline WHERE symbol=? AND interval=? AND atr14 IS NOT NULL "
        "AND t < ? ORDER BY t DESC LIMIT 1", (symbol, interval, int(bar.t))).fetchone()
    if row is not None:
        prev_c = conn.execute(
            "SELECT c FROM kline WHERE symbol=? AND interval=? AND t < ? "
            "ORDER BY t DESC LIMIT 1", (symbol, interval, int(bar.t))).fetchone()
        if prev_c is not None:
            pc = float(prev_c[0])
            tr = max(float(bar.h) - float(bar.l), abs(float(bar.h) - pc), abs(float(bar.l) - pc))
            out["atr14"] = _round8((float(row[0]) * (_ATR_PERIOD - 1) + tr) / _ATR_PERIOD)
    return out


class SeriesRegistry:
    """每个 (symbol, interval) 一个写状态；watcher 全局复用一个 registry。"""

    def __init__(self, specs: Iterable, engine: str = "legacy",
                 shadow_tol: float = 1e-6) -> None:
        self.specs = list(specs)
        self.engine = resolve_engine(engine) if engine in (None, "") else engine
        if self.engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
        self._writers: dict[tuple[str, str], SeriesWriter] = {}
        self.stats = ShadowStats(tol=shadow_tol) if self.engine == "shadow" else None

    # ── 状态 ────────────────────────────────────────────────
    def writer(self, symbol: str, interval: str) -> SeriesWriter:
        key = (symbol, interval)
        w = self._writers.get(key)
        if w is None:
            w = SeriesWriter(self.specs)
            self._writers[key] = w
        return w

    def prime(self, conn: sqlite3.Connection, symbol: str, interval: str) -> int:
        """用库里最近的 warmup 根预热新引擎状态（重连/重启后避免从 SMA 重播种）。"""
        warmup = max((s.warmup for s in self.specs), default=0)
        return self.writer(symbol, interval).seed_from_db(conn, symbol, interval, warmup)

    def shadow_summary(self) -> Optional[dict]:
        return self.stats.summary() if self.stats else None

    # ── 写一根 ──────────────────────────────────────────────
    def write(self, conn: sqlite3.Connection, symbol: str, interval: str, bar: Bar,
              now: Optional[float] = None) -> dict:
        if self.engine == "legacy":
            vals = legacy_values(conn, symbol, interval, bar)
            upsert_bar(conn, symbol, interval, bar,
                       indicator_cols=("ema20", "atr14"), values=vals,
                       present=table_columns(conn))
            return {"engine": "legacy", "written": True, "values": vals}

        w = self.writer(symbol, interval)
        # ── 首次写入前预热（2026-10-28 实测事故）────────────────────────
        # `prime()` 此前**没有任何调用者**：v2 引擎启动时 StreamState 是空的，
        # 于是前 20~200 根新 bar 的指标列全 NULL（实测 21:42/21:43 两根 0/7 填充）。
        # 每序列只在首写时 seed 一次，代价是读 warmup 根 OHLC。
        if not getattr(w, "_primed", False):
            try:
                seeded = self.prime(conn, symbol, interval)
                logger.info("primed %s %s: %d 根（engine=%s）",
                            symbol, interval, seeded, self.engine)
            except Exception as e:  # noqa: BLE001 — 预热失败不阻断写入
                logger.warning("prime %s %s failed: %s", symbol, interval, e)
            finally:
                w._primed = True

        if self.engine == "shadow":
            # 旧值入库（下游零影响），新引擎只推进状态 + 比对
            legacy = legacy_values(conn, symbol, interval, bar)
            upsert_bar(conn, symbol, interval, bar,
                       indicator_cols=("ema20", "atr14"), values=legacy,
                       present=w.columns(conn))
            new = w.compute(bar)
            if self.stats is not None:
                self.stats.record_row(int(bar.t), new, legacy,
                                      columns=[c for c in new if c in legacy])
            return {"engine": "shadow", "written": True, "values": legacy, "shadow": new}

        res = w.write(conn, symbol, interval, bar, now=now)
        res.values.setdefault("t", int(bar.t))
        return {"engine": "v2", "written": res.written, "closed": res.closed,
                "values": res.values, "skipped": res.skipped_reason}
