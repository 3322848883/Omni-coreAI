"""深回填：`to` 游标分页（突破单次 1000 根限制）+ 7d/30d 兜底合成。

实测依据（2026-10-27）
- Gate `candlesticks` 单次 `limit ≤ 1000`，但支持 `to=<秒>` 游标：
  1d 三页拉出 2,534 根全史（2019-11-20 起），每页多 1 根重叠 → 必须按 `t` 去重。
- 低周期（1s/10s/30s）受交易所保留期限制，分页也拿不到更早（1s 仅 ~17 分钟）。

设计
- `fetch_paged(fetch_fn)` 只依赖「(interval, limit, to) → rows」的可注入函数，便于单测；
- `synthesize()` 由低周期合成高周期（只在官方不可用时兜底，标 src='synthetic'）；
- 合成的不做指标（由 `recalc_series` 统一补），避免两套口径。
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from writer_v2 import SRC_BACKFILL, SRC_SYNTH, Bar, interval_seconds

__all__ = ["MAX_PAGE", "fetch_paged", "synthesize", "to_bars", "covered_span"]

MAX_PAGE = 1000


def fetch_paged(fetch_fn: Callable[..., list], interval: str, target: int,
                to: Optional[int] = None, page: int = MAX_PAGE,
                max_pages: int = 20, sleep: Callable[[float], None] = lambda s: None,
                sleep_sec: float = 0.25) -> list[dict]:
    """循环向后翻页，返回按 t 升序、去重后的 rows（最多 target 根）。

    `fetch_fn(interval, limit, to)` 需返回 dict 行（含 t/o/h/l/c/v/sum）。
    """
    seen: dict[int, dict] = {}
    cursor = to
    pages = 0
    while len(seen) < target and pages < max_pages:
        rows = fetch_fn(interval, min(page, MAX_PAGE), cursor)
        pages += 1
        if not rows:
            break
        new = 0
        for r in rows:
            t = int(r["t"])
            if t not in seen:
                seen[t] = r
                new += 1
        if new == 0:  # 没有新数据了（到底或接口忽略 to）
            break
        earliest = min(int(r["t"]) for r in rows)
        if cursor is not None and earliest >= cursor:
            break
        cursor = earliest - 1
        if sleep_sec:
            sleep(sleep_sec)
    ordered = [seen[t] for t in sorted(seen)]
    return ordered[-target:] if target and len(ordered) > target else ordered


def synthesize(rows: Iterable[dict], src_interval: str, dst_interval: str) -> list[Bar]:
    """把低周期 rows 聚合成高周期 Bar 列表。

    聚合口径：`o=first, h=max, l=min, c=last, v=sum, sum=sum`，
    按 `floor(t / dst_sec) * dst_sec` 分桶；**不完整桶**（分量数不足）丢弃 ——
    与 Gate 官方 bar 的语义一致（实测官方 vs 合成在完整桶上 OHLC 差 0.0000）。
    """
    src_sec = interval_seconds(src_interval)
    dst_sec = interval_seconds(dst_interval)
    if dst_sec % src_sec != 0:
        raise ValueError(f"{dst_interval} 不是 {src_interval} 的整数倍，无法合成")
    need = dst_sec // src_sec
    buckets: dict[int, dict] = {}
    for r in rows:
        t = int(r["t"])
        key = (t // dst_sec) * dst_sec
        b = buckets.get(key)
        if b is None:
            buckets[key] = {"t": key, "o": float(r["o"]), "h": float(r["h"]),
                            "l": float(r["l"]), "c": float(r["c"]),
                            "v": float(r.get("v") or 0.0),
                            "sum": float(r.get("sum") or 0.0), "n": 1}
            continue
        b["h"] = max(b["h"], float(r["h"]))
        b["l"] = min(b["l"], float(r["l"]))
        b["c"] = float(r["c"])
        b["v"] += float(r.get("v") or 0.0)
        b["sum"] += float(r.get("sum") or 0.0)
        b["n"] += 1
    out: list[Bar] = []
    for key in sorted(buckets):
        b = buckets[key]
        if b["n"] != need:
            continue
        out.append(Bar(t=b["t"], o=b["o"], h=b["h"], l=b["l"], c=b["c"],
                       v=b["v"], sum=b["sum"], closed=True, src=SRC_SYNTH))
    return out


def to_bars(rows: Iterable[dict], interval: str, src: str = SRC_BACKFILL,
            drop_unclosed_tail: bool = True, now: Optional[float] = None) -> list[Bar]:
    """把 REST 行转 Bar；`interval` 决定是否丢掉「最后一根可能未收线」的那根。"""
    import time as _t
    now = _t.time() if now is None else now
    bars = []
    rows = sorted(rows, key=lambda r: int(r["t"]))
    for i, r in enumerate(rows):
        t = int(r["t"])
        last = i == len(rows) - 1
        if drop_unclosed_tail and last and t + interval_seconds(interval) > now:
            continue
        bars.append(Bar(t=t, o=float(r["o"]), h=float(r["h"]), l=float(r["l"]),
                        c=float(r["c"]), v=float(r.get("v") or 0.0),
                        sum=float(r.get("sum") or 0.0), closed=True, src=src))
    return bars


def covered_span(rows: Iterable[dict], interval: str) -> dict:
    rows = list(rows)
    if not rows:
        return {"n": 0, "days": 0.0}
    sec = interval_seconds(interval)
    ts = [int(r["t"]) for r in rows]
    return {"n": len(rows), "days": (max(ts) - min(ts) + sec) / 86400.0}
