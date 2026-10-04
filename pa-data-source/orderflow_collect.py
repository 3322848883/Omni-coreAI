"""订单流采集器 —— 把 WS 消息转成聚合结果并落库。

集成方式（复用 kline_watcher 已建的 Gate WS 连接，不另开连接）：
  1. `on_open` 里追加订阅三个频道
  2. `on_message` 里在「非 K 线频道直接 return」**之前**调用 `collector.on_message(msg)`
  3. 后台线程每秒调 `collector.flush()`

职责边界：本模块只做「消息 → 聚合 → 落库」，不碰连接、不碰重连。
连接与重连仍归 kline_watcher，断线时本采集器只需接受「数据有缺口」这一事实
（WS 无历史可回填，缺口不补）。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from orderflow_agg import (
    OrderBookState,
    UpdateResult,
    apply_snapshot,
    apply_update,
    book_metrics,
    footprint_bucket,
    sequence_ok,
    tape_bucket,
)

import orderflow_db


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class ContractState:
    """单合约的采集状态。"""

    def __init__(self, contract: str, depth_n: int = 5,
                 big_window_sec: float = 300.0, default_big: float = 1000.0):
        self.contract = contract
        self.book = OrderBookState()
        self.depth_n = depth_n
        self.big_window_sec = big_window_sec
        self.default_big = default_big

        self.trades: list = []              # 本周期逐笔
        self.traded_by_price: dict = {}     # 本周期各价位成交量（撤单归因用）
        self.pending = UpdateResult()       # 本周期累积的挂/撤量
        self.pending_walls: list = []       # 本周期结算的长寿挂单
        self.depth_hist: list = []          # 深度历史（算均值）
        self.trade_hist: list = []          # (ts, size) 算大单分位
        self.fp_accum: dict = {}            # footprint 累积（分钟级）
        self.prev_total = 0.0               # 上一轮簿总量（撤单率的分母）
        self.last_tape_ts = 0
        self.last_book_ts = 0
        self.last_fp_ts = 0
        self.resync_needed = False          # 序列号断号 → 需重拉快照

    # ── 大单阈值：动态分位 ────────────────────────────
    def big_threshold(self, now: Optional[float] = None) -> float:
        """近 5 分钟成交量的 90 分位。样本不足时退回默认值。"""
        now = now or time.time()
        sizes = [s for t, s in self.trade_hist if now - t <= self.big_window_sec]
        if len(sizes) < 50:
            return self.default_big
        sizes.sort()
        return sizes[int(len(sizes) * 0.9)]

    def depth_mean(self) -> Optional[float]:
        if len(self.depth_hist) < 20:
            return None
        return sum(self.depth_hist) / len(self.depth_hist)


class OrderFlowCollector:
    """多合约订单流采集。"""

    def __init__(self, contracts: list, db_path: str, *, depth: int = 20,
                 depth_n: int = 5, tape_sec: int = 1, book_sec: int = 5,
                 footprint_sec: int = 60, purge_sec: int = 3600,
                 default_big: float = 1000.0):
        self.contracts = list(contracts)
        self.depth = depth
        self.tape_sec = tape_sec
        self.book_sec = book_sec
        self.footprint_sec = footprint_sec
        self.purge_sec = purge_sec
        self.states = {c: ContractState(c, depth_n=depth_n,
                                        default_big=default_big)
                       for c in self.contracts}
        self.conn = orderflow_db.connect(db_path)
        self.last_purge = 0
        self.errors: list = []
        # SQLite 连接跨线程使用必须串行化（连接在主线程建、flush 在后台线程跑）
        self._lock = threading.Lock()

    # ── 订阅报文（供 kline_watcher 的 on_open 使用）────────
    def subscriptions(self) -> list:
        """返回需要发送的订阅报文列表（不含 time 字段，由调用方补）。"""
        out = []
        for c in self.contracts:
            out.append({"channel": "futures.order_book", "event": "subscribe",
                        "payload": [c, str(self.depth), "0"]})
            out.append({"channel": "futures.order_book_update", "event": "subscribe",
                        "payload": [c, "100ms", str(self.depth)]})
            out.append({"channel": "futures.trades", "event": "subscribe",
                        "payload": [c]})
        return out

    # ── 消息入口 ────────────────────────────────────
    def on_message(self, raw: Any) -> bool:
        """处理一条 WS 消息。返回 True 表示是本采集器消费的频道。"""
        if isinstance(raw, (str, bytes)):
            try:
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                return False
        elif isinstance(raw, dict):
            data = raw
        else:
            return False

        channel = data.get("channel") or ""
        if channel not in ("futures.order_book", "futures.order_book_update",
                           "futures.trades"):
            return False
        if data.get("event") == "subscribe":
            return True
        result = data.get("result")
        if channel == "futures.order_book":
            self._on_snapshot(result)
        elif channel == "futures.order_book_update":
            self._on_update(result)
        else:
            self._on_trades(result)
        return True

    def _state_of(self, contract: str) -> Optional[ContractState]:
        return self.states.get(contract)

    def _on_snapshot(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        st = self._state_of(result.get("contract") or "")
        if st is None:
            return
        apply_snapshot(st.book, result.get("bids") or [], result.get("asks") or [],
                       ts=time.time(), seq=result.get("id"))
        st.resync_needed = False

    def _on_update(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        st = self._state_of(result.get("s") or result.get("contract") or "")
        if st is None:
            return
        if not sequence_ok(st.book, result.get("U")):
            # 断号 → 本地簿已漂移，标记重拉快照；本次增量丢弃
            st.resync_needed = True
            self.errors.append(("seq_gap", st.contract, result.get("U")))
            return
        now = time.time()
        r1 = apply_update(st.book, result.get("b") or [], True, ts=now,
                          traded_by_price=st.traded_by_price, seq=result.get("u"))
        r2 = apply_update(st.book, result.get("a") or [], False, ts=now,
                          traded_by_price=st.traded_by_price)
        for r in (r1, r2):
            st.pending.place_vol += r.place_vol
            st.pending.cancel_vol += r.cancel_vol
            st.pending.eaten_vol += r.eaten_vol
            st.pending_walls.extend(r.closed_walls)

    def _on_trades(self, result: Any) -> None:
        rows = result if isinstance(result, list) else [result]
        now = time.time()
        for t in rows:
            if not isinstance(t, dict):
                continue
            st = self._state_of(t.get("contract") or "")
            if st is None:
                continue
            sz = _f(t.get("size"))
            p = _f(t.get("price"))
            st.trades.append({"price": p, "size": sz})
            st.traded_by_price[p] = st.traded_by_price.get(p, 0.0) + abs(sz)
            st.trade_hist.append((now, abs(sz)))

    # ── 定时聚合落库 ────────────────────────────────
    def flush(self, now: Optional[float] = None) -> dict:
        now = now or time.time()
        written = {"tape": 0, "book": 0, "footprint": 0, "walls": 0}
        with self._lock:
            for st in self.states.values():
                self._flush_tape(st, now, written)
                self._flush_book(st, now, written)
                self._flush_footprint(st, now, written)
            if now - self.last_purge >= self.purge_sec:
                orderflow_db.purge(self.conn, int(now))
                self.last_purge = now
            self.conn.commit()
        return written

    def _flush_tape(self, st: ContractState, now: float, written: dict) -> None:
        if now - st.last_tape_ts < self.tape_sec:
            return
        if not st.trades:
            st.last_tape_ts = now
            # 无成交也要清 traded_by_price，否则撤单归因会用到过期成交量
            st.traded_by_price = {}
            return
        bucket = tape_bucket(st.trades, st.big_threshold(now))
        orderflow_db.write_tape(self.conn, int(now), st.contract, bucket)
        written["tape"] += 1

        for p, (b, s) in footprint_bucket(st.trades, tick=0.1).items():
            slot = st.fp_accum.setdefault(p, [0.0, 0.0])
            slot[0] += b
            slot[1] += s

        st.trades = []
        st.traded_by_price = {}
        st.last_tape_ts = now
        # 清理过期的成交历史（只保留大单窗口所需）
        cut = now - st.big_window_sec * 2
        st.trade_hist = [(t, s) for t, s in st.trade_hist if t >= cut]

    def _flush_book(self, st: ContractState, now: float, written: dict) -> None:
        if now - st.last_book_ts < self.book_sec:
            return
        depth5 = st.book.depth(st.depth_n, True) + st.book.depth(st.depth_n, False)
        st.depth_hist.append(depth5)
        if len(st.depth_hist) > 10000:
            st.depth_hist = st.depth_hist[-10000:]

        traded = st.pending.eaten_vol
        metrics = book_metrics(st.book, st.depth_mean(), depth_n=st.depth_n,
                               window_sec=self.book_sec, res=st.pending,
                               prev_total=st.prev_total, traded_vol=traded)
        orderflow_db.write_book_state(self.conn, int(now), st.contract, metrics)
        written["book"] += 1
        # 撤单率的分母：本轮的簿总量，供下一轮使用
        st.prev_total = st.book.total_size()

        if st.pending_walls:
            orderflow_db.write_walls(self.conn, int(now), st.contract, st.pending_walls)
            written["walls"] += len(st.pending_walls)
            st.pending_walls = []

        st.pending = UpdateResult()
        st.last_book_ts = now

    def _flush_footprint(self, st: ContractState, now: float, written: dict) -> None:
        if now - st.last_fp_ts < self.footprint_sec:
            return
        if st.fp_accum:
            orderflow_db.write_footprint(self.conn, int(now), st.contract, st.fp_accum)
            written["footprint"] += len(st.fp_accum)
            st.fp_accum = {}
        st.last_fp_ts = now

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
