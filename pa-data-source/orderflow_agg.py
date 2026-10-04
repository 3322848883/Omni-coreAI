"""订单流聚合 —— 把 WS 原始增量/逐笔压成可落库、可判读的指标。

设计原则：
  - **纯函数 + 显式状态**：实时流不可复现，只有把聚合逻辑做成纯函数才能用
    构造序列做单元测试。全局状态只存在于采集器，不在这里。
  - **撤单 vs 成交的归因单独成函数**：增量只给「挂单量差」，不给原因，
    这是整条链最容易错的地方（把撤单算成成交会虚增成交量）。

判据来源：
  - 撤单率 = 消失挂单量 / 上一快照总挂单量（业界口径）
  - 深度 = 前 N 档累计 / 滚动均值；<30% 算薄盘
  - 相对价差 = (ask1 - bid1) / mid；>0.05% 时微观信号基本可忽略
  - 挂单存活 >30s 才有参考价值（本机实测中位存活仅 4.3s、85% 变化是撤单）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 盘口状态分级阈值。**单位是小数**（0.0002 = 0.02%），与 relative_spread()
# 的返回值一致；业界的「价差 <0.02%」即此处的 0.0002。
GRADE_TABLE = (
    ("excellent", 0.0002, 0.80),      # 价差 <0.02% 且 深度 >80%
    ("normal", 0.0005, 0.50),         # <0.05% 且 >50%
    ("poor", 0.0010, 0.30),           # <0.10% 且 >30%
    ("bad", float("inf"), 0.0),
)

WALL_MIN_AGE_SEC = 30.0   # 长寿挂单门槛（业界经验值）


@dataclass
class BookLevel:
    """单个价位的挂单状态。"""
    size: float
    born_ts: float                      # 首次出现（或从 0 恢复）的时间
    peak_size: float = 0.0              # 曾达到的最大挂单量

    def age(self, now: float) -> float:
        return max(now - self.born_ts, 0.0)


@dataclass
class OrderBookState:
    """本地订单簿。key 用 float 价位，便于与逐笔成交价对齐。"""
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    last_u: Optional[int] = None

    def side(self, is_bid: bool) -> dict:
        return self.bids if is_bid else self.asks

    def total_size(self) -> float:
        return sum(lv.size for lv in self.bids.values()) + \
               sum(lv.size for lv in self.asks.values())

    def depth(self, n: int, is_bid: bool) -> float:
        """前 n 档累计挂单量（bid 从高到低，ask 从低到高）。"""
        book = self.side(is_bid)
        prices = sorted(book.keys(), reverse=is_bid)[:n]
        return sum(book[p].size for p in prices)

    def best(self) -> tuple[Optional[float], Optional[float]]:
        if not self.bids or not self.asks:
            return None, None
        return max(self.bids.keys()), min(self.asks.keys())


def sequence_ok(state: OrderBookState, first_u: Optional[int]) -> bool:
    """增量序列号是否连续。不连续说明丢包，本地簿已漂移，必须重拉快照。"""
    if first_u is None or state.last_u is None:
        return True
    return first_u <= state.last_u + 1


def attribute_decrease(old_size: float, new_size: float,
                       traded_vol: float) -> tuple[float, float]:
    """把「挂单量减少」归因为 (被吃, 被撤)。

    增量只给挂单量差，不给原因。用同价位、同窗口的成交量交叉校验：
      - 减少量 <= 成交量 → 全部视为被吃
      - 减少量 >  成交量 → 成交量部分算被吃，差额算撤单

    注意：大单可能吃穿多档，成交量会分摊到多个价位；调用方应按价位分别传入
    该价位的成交量，而不是整窗总量。
    """
    decrease = max(old_size - new_size, 0.0)
    eaten = min(decrease, max(traded_vol, 0.0))
    cancelled = max(decrease - max(traded_vol, 0.0), 0.0)
    return eaten, cancelled


def apply_snapshot(state: OrderBookState, bids: list, asks: list,
                   ts: float, seq: Optional[int] = None) -> None:
    """用快照覆盖本地簿。已有价位保留原 born_ts（避免存活时间被快照重置）。"""
    for side_key, rows in (("bids", bids), ("asks", asks)):
        book = getattr(state, side_key)
        new: dict = {}
        for r in rows or []:
            p = float(r["p"])
            s = float(r["s"])
            if s <= 0:
                continue
            old = book.get(p)
            if old is not None:
                new[p] = BookLevel(s, old.born_ts, max(old.peak_size, s))
            else:
                new[p] = BookLevel(s, ts, s)
        setattr(state, side_key, new)
    if seq is not None:
        state.last_u = seq


@dataclass
class UpdateResult:
    """一次增量带来的变化量。"""
    place_vol: float = 0.0        # 新增挂单量
    cancel_vol: float = 0.0       # 撤单量
    eaten_vol: float = 0.0        # 被吃掉的量（有成交量佐证）
    closed_walls: list = field(default_factory=list)   # 结算的长寿挂单


def apply_update(state: OrderBookState, changes: list, is_bid: bool,
                 ts: float, traded_by_price: Optional[dict] = None,
                 seq: Optional[int] = None) -> UpdateResult:
    """应用一侧的增量。

    `changes` 形如 `[{"p": "84918.1", "s": 2643}]`；`s == 0` 表示该档消失。
    `traded_by_price` 是窗口内该价位的成交量，用于区分被吃与撤单。
    """
    res = UpdateResult()
    book = state.side(is_bid)
    traded_by_price = traded_by_price or {}
    for c in changes or []:
        p = float(c["p"])
        s = float(c["s"])
        old = book.get(p)
        old_size = old.size if old is not None else 0.0

        if s <= 0:
            # 该档消失：按成交量归因
            eaten, cancelled = attribute_decrease(old_size, 0.0,
                                                  traded_by_price.get(p, 0.0))
            res.eaten_vol += eaten
            res.cancel_vol += cancelled
            if old is not None and old.age(ts) >= WALL_MIN_AGE_SEC:
                res.closed_walls.append({
                    "price": p, "side": "bid" if is_bid else "ask",
                    "peak_size": old.peak_size, "age_sec": round(old.age(ts), 1),
                    "outcome": "eaten" if eaten >= cancelled else "cancelled",
                })
            book.pop(p, None)
            continue

        if old is None:
            res.place_vol += s
            book[p] = BookLevel(s, ts, s)
        elif s > old_size:
            res.place_vol += (s - old_size)
            old.size = s
            old.peak_size = max(old.peak_size, s)
        elif s < old_size:
            eaten, cancelled = attribute_decrease(old_size, s,
                                                  traded_by_price.get(p, 0.0))
            res.eaten_vol += eaten
            res.cancel_vol += cancelled
            old.size = s
        # s == old_size → 无变化
    if seq is not None:
        state.last_u = seq
    return res


def relative_spread(state: OrderBookState) -> Optional[float]:
    """相对价差 = (ask1 - bid1) / mid。"""
    bid, ask = state.best()
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid


def grade(spread_pct: Optional[float], depth_ratio: Optional[float]) -> str:
    """盘口状态分级。`spread_pct` 与 `depth_ratio` 均为小数（0.0005 = 0.05%）。"""
    if spread_pct is None:
        return "unknown"
    for name, sp_lim, dp_lim in GRADE_TABLE:
        if spread_pct < sp_lim:
            # 价差达标后还要看深度（depth_ratio 未知时按达标处理）
            if depth_ratio is None or depth_ratio > dp_lim:
                return name
    return "bad"


def book_metrics(state: OrderBookState, depth_hist_mean: Optional[float],
                 depth_n: int = 5, window_sec: float = 5.0,
                 res: Optional[UpdateResult] = None,
                 prev_total: Optional[float] = None,
                 traded_vol: float = 0.0) -> dict:
    """合成一行盘口状态指标（对应 of_book_state 表）。"""
    bid5 = state.depth(depth_n, True)
    ask5 = state.depth(depth_n, False)
    depth5 = bid5 + ask5
    depth_ratio = None
    if depth_hist_mean and depth_hist_mean > 0:
        depth_ratio = depth5 / depth_hist_mean

    sp = relative_spread(state)
    cancel_vol = res.cancel_vol if res else 0.0
    place_vol = res.place_vol if res else 0.0
    cancel_rate = None
    if prev_total and prev_total > 0:
        cancel_rate = cancel_vol / prev_total

    return {
        "spread_pct": None if sp is None else round(sp * 100.0, 6),
        "depth_bid": round(bid5, 2),
        "depth_ask": round(ask5, 2),
        "depth_ratio": None if depth_ratio is None else round(depth_ratio, 4),
        "place_vol": round(place_vol, 2),
        "cancel_vol": round(cancel_vol, 2),
        "cancel_rate": None if cancel_rate is None else round(cancel_rate, 4),
        "traded_vol": round(traded_vol, 2),
        "intensity": round(traded_vol / window_sec, 2) if window_sec > 0 else None,
        "grade": grade(sp, depth_ratio),
        "levels": len(state.bids) + len(state.asks),
    }


def tape_bucket(trades: list, big_threshold: float) -> dict:
    """把一段逐笔成交聚成一行（对应 of_tape 表）。

    `trades` 形如 `[{"price": "84945.2", "size": -8198}]`，size 符号即方向。
    """
    buy = sell = 0.0
    big_n = 0
    big_sz = 0.0
    biggest = 0.0
    for t in trades or []:
        try:
            sz = float(t.get("size") or 0)
        except (TypeError, ValueError):
            continue
        a = abs(sz)
        if sz > 0:
            buy += sz
        else:
            sell += a
        if a >= big_threshold:
            big_n += 1
            big_sz += a
        if a > biggest:
            biggest = a
    return {
        "buy_size": round(buy, 2),
        "sell_size": round(sell, 2),
        "delta": round(buy - sell, 2),
        "big_count": big_n,
        "big_size": round(big_sz, 2),
        "max_trade": round(biggest, 2),
    }


def footprint_bucket(trades: list, tick: float) -> dict:
    """把逐笔按价位（tick 对齐）聚成 footprint。

    返回 `{price: [buy, sell]}`，price 按 tick 取整。
    """
    out: dict = {}
    if tick <= 0:
        return out
    for t in trades or []:
        try:
            p = float(t.get("price") or 0)
            sz = float(t.get("size") or 0)
        except (TypeError, ValueError):
            continue
        key = round(round(p / tick) * tick, 8)
        slot = out.setdefault(key, [0.0, 0.0])
        if sz > 0:
            slot[0] += sz
        else:
            slot[1] += abs(sz)
    return out
