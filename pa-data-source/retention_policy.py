"""分周期保留策略：按周期设 `max_candles` / `days`，带守卫与 dry-run。

为什么不用旧的全局单值
- 旧实现 `retention_days=300` + `max_candles=2000` 是**全局**的，导致
  1d 只剩 300 根（300 天 × 1 根/天）、4h 只剩 1800 根 —— 长周期被短周期策略连坐，
  SMC 预热（500）都不够。
- 新策略：**每周期独立**，并强制两条不变量：
  `max_candles >= warmup`（ema200 需 1005）、`days*86400/interval >= 0.9*max_candles`
  （防止「天数」先把行数删光）。

守卫：`DELETE ... WHERE t < now - max(days*86400, 2*interval)`，避免时钟漂移把最新几根删掉。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from writer_v2 import interval_seconds

__all__ = ["Limits", "DEFAULT_LIMITS", "parse_limits", "validate", "truncate",
           "apply_retention"]

MAX_BACKFILL_DEPTH = {
    # Gate 单次 limit≤1000 的可回溯深度（根），用于「能回填多少」而不是「留多少」
    "1m": 1000, "3m": 1000, "5m": 1000, "15m": 1000, "30m": 1000,
    "1h": 1000, "2h": 1000, "4h": 1000, "6h": 1000, "8h": 1000,
    "12h": 1000, "1d": 1500, "7d": 1000, "30d": 1000,
}


@dataclass(frozen=True)
class Limits:
    max_candles: int
    days: int
    depth_bars: int = 1000


DEFAULT_LIMITS: dict[str, Limits] = {
    "1m": Limits(5_760, 4, 1_000),
    "5m": Limits(8_640, 30, 1_000),
    "15m": Limits(8_640, 90, 1_000),
    "30m": Limits(8_640, 180, 1_000),
    "1h": Limits(8_760, 365, 1_000),
    "4h": Limits(2_000, 333, 1_000),
    "1d": Limits(1_600, 1_600, 1_500),
}


def parse_limits(raw: Optional[dict], warmup: int = 0) -> dict[str, Limits]:
    """把 yaml 的 `retention.per_interval` 解析成 Limits；缺省回落到 DEFAULT_LIMITS。"""
    out: dict[str, Limits] = {}
    raw = raw or {}
    for interval, default in DEFAULT_LIMITS.items():
        item = raw.get(interval) or {}
        if isinstance(item, int):
            item = {"max_candles": item}
        out[interval] = Limits(
            max_candles=int(item.get("max_candles", default.max_candles)),
            days=int(item.get("days", default.days)),
            depth_bars=int(item.get("depth_bars", default.depth_bars)),
        )
    for interval in raw:
        if interval not in out:
            item = raw[interval] if isinstance(raw[interval], dict) else {}
            out[interval] = Limits(
                max_candles=int(item.get("max_candles", 2000)),
                days=int(item.get("days", 300)),
                depth_bars=int(item.get("depth_bars", 1000)),
            )
    if warmup:
        validate(out, warmup)
    return out


def validate(limits: dict[str, Limits], warmup: int) -> None:
    """两条硬不变量，违反直接报错（不许静默删数据）。"""
    for interval, lim in limits.items():
        sec = interval_seconds(interval)
        if lim.max_candles < warmup:
            raise ValueError(
                f"{interval}: max_candles={lim.max_candles} < 指标预热 {warmup}，"
                "该周期永远算不出长周期指标")
        if lim.days > 0:
            days_capacity = lim.days * 86400 / sec
            if days_capacity < lim.max_candles * 0.9:
                raise ValueError(
                    f"{interval}: days={lim.days} 只能容纳 {days_capacity:.0f} 根，"
                    f"小于 max_candles={lim.max_candles} 的 90%；先调大 days")


def truncate(conn: sqlite3.Connection, symbol: str, interval: str, max_candles: int,
             dry_run: bool = False) -> int:
    """按行数截断（保留最新 max_candles 根）。"""
    if max_candles <= 0:
        return 0
    total = conn.execute(
        "SELECT count(*) FROM kline WHERE symbol=? AND interval=?", (symbol, interval)
    ).fetchone()[0]
    if total <= max_candles:
        return 0
    to_delete = total - max_candles
    if dry_run:
        return to_delete
    conn.execute(
        "DELETE FROM kline WHERE symbol=? AND interval=? AND t < ("
        "  SELECT min(t) FROM (SELECT t FROM kline WHERE symbol=? AND interval=?"
        "                      ORDER BY t DESC LIMIT ?))",
        (symbol, interval, symbol, interval, int(max_candles)),
    )
    return to_delete


def apply_retention(conn: sqlite3.Connection, symbol: str, interval: str, lim: Limits,
                    now: Optional[float] = None, dry_run: bool = False) -> dict:
    """时间 + 行数双约束；守卫保证至少保留最近 2 根。"""
    import time as _t
    now = _t.time() if now is None else now
    sec = interval_seconds(interval)
    deleted_time = 0
    if lim.days > 0:
        span = max(lim.days * 86400, 2 * sec)
        cutoff = int(now - span)
        if dry_run:
            deleted_time = conn.execute(
                "SELECT count(*) FROM kline WHERE symbol=? AND interval=? AND t<?",
                (symbol, interval, cutoff)).fetchone()[0]
        else:
            cur = conn.execute(
                "DELETE FROM kline WHERE symbol=? AND interval=? AND t<?",
                (symbol, interval, cutoff))
            deleted_time = int(cur.rowcount or 0)
    deleted_rows = truncate(conn, symbol, interval, lim.max_candles, dry_run=dry_run)
    return {"deleted_time": deleted_time, "deleted_rows": deleted_rows}
