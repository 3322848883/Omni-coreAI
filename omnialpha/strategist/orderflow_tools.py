"""订单流工具（读 pa-data-source 的 orderflow.db）。

与 `tv_tools.py` 的差别：那批是 **K 线几何估算**（对齐 TV 原版、零依赖），
这批是 **WS 实采的微观数据**（逐笔方向、挂单存活、撤单率）。

四个工具：
  orderflow_tape       逐秒主动买卖量 / Delta / 大单
  orderflow_footprint  逐价位真实买卖量（替代几何估算）
  orderbook_state      盘口状态四维度 + 分级（信号可信度的前置门）
  orderbook_walls      长寿挂单（吸收判据的核心输入）

数据源不在时统一返回明确的错误，不静默给空。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from .symbols import resolve_symbol_arg, symbol_error_payload  # noqa: E402
from typing import Any, Optional

ORDERFLOW_TOOL_NAMES = (
    "orderflow_tape",
    "orderflow_footprint",
    "orderbook_state",
    "orderbook_walls",
)

_SYM = {"type": "string", "description": "币种（取【品种宇宙】里的值）"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 500,
          "description": "how many recent rows (default 60)"}

ORDERFLOW_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "orderflow_tape",
            "description": (
                "Real taker tape aggregated per second from the exchange WebSocket: "
                "`totals` (buy_size / sell_size / delta / cvd / big_count), `cvd_tail`, and "
                "`rows_detail`. Large-trade threshold = 90th percentile of the last 5 minutes. "
                "Direction comes from the trade sign, so these are measured tick values — "
                "prefer them over tv_cdv's candle-geometry estimate. CVD is more meaningful "
                "than a single bar (a single bar only shows the dynamic shift)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"symbol": _SYM, "limit": _LIMIT},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "orderflow_footprint",
            "description": (
                "Real footprint: per-price taker buy/sell volume over the last `minutes`, "
                "returning `levels[]` (price / buy / sell / delta / buy_ratio), `poc` "
                "(highest-volume price) and `imbalance_levels`. Imbalance criterion: a single "
                "level counts when the buy/sell ratio is >= 3:1; a STACK requires >= 3 "
                "consecutive same-direction levels. Measured from ticks — prefer it over "
                "tv_vol_oi_footprint's geometry split."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM,
                    "minutes": {"type": "integer", "minimum": 1, "maximum": 240,
                                "description": "lookback minutes (default 15)"},
                    "rows": {"type": "integer", "minimum": 3, "maximum": 60,
                             "description": "top-N price levels to return (default 15)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "orderbook_state",
            "description": (
                "Order-book state (the pre-filter for every micro signal). Four dimensions: "
                "relative spread, top-5 depth, cancel rate, trade intensity — plus a grade. "
                "The grade thresholds (spread as a fraction; depth = current / historical mean) "
                "are: excellent = spread <0.0002 (0.02%) and depth >0.80; normal = <0.0005 and "
                ">0.50; poor = <0.0010 and >0.30; bad = anything worse. When the grade is "
                "poor/bad, micro-structure signals must be down-weighted or skipped — a thin "
                "book exaggerates every 'large order'. Also skip micro signals when spread_pct "
                "is >2x its recent average, or when volatility spikes. Note: `depth_ratio` / "
                "`cancel_rate` can be null early after collection starts (insufficient sample) "
                "— fall back to `grade` then."
            ),
            "parameters": {
                "type": "object",
                "properties": {"symbol": _SYM, "limit": _LIMIT},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "orderbook_walls",
            "description": (
                "Long-lived limit orders (age >= min_age seconds) that were finally eaten or "
                "cancelled. Only long-lived walls carry information — measured median order "
                "lifetime is ~4s and >80% of book changes are cancellations, so short-lived "
                "quotes are treated as spoofing and excluded."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": _SYM,
                    "min_age": {"type": "number", "minimum": 0, "maximum": 3600,
                                "description": "minimum lifetime in seconds (default 30)"},
                    "limit": _LIMIT,
                },
                "required": ["symbol"],
            },
        },
    },
]


def _orderflow_db(bot_root=None) -> Optional[Path]:
    """解析 pa-data-source/data/orderflow.db 的路径。"""
    import os

    env = os.environ.get("OMNIALPHA_ORDERFLOW")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    bases = []
    if bot_root:
        bases.append(Path(bot_root) / "pa-data-source" / "data" / "orderflow.db")
    bases.append(Path.cwd() / "pa-data-source" / "data" / "orderflow.db")
    for p in bases:
        if p.is_file():
            return p
    return None


def _q(bot_root, sql: str, params: tuple = ()) -> list:
    db = _orderflow_db(bot_root)
    if not db:
        return [{"error": "orderflow.db not found (is kline_watcher running with "
                          "orderflow collection enabled?)"}]
    try:
        conn = sqlite3.connect(str(db), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return [{"error": f"orderflow query failed: {e}"}]


def run_orderflow_tool(bot_root, name: str, args: dict,
                       symbols: Optional[list] = None) -> Any:
    """执行订单流工具。

    symbol 走 `strategist/symbols.py`（唯一解析来源，T6）：单币宇宙自动补，
    多币/越界/未配置一律拒绝。原来只判"非空"，于是 `BTC` / `BTCUSDT` 这类写法
    会命中 0 行、**静默返回空**（看起来像"这个币没有订单流数据"）。
    """
    args = dict(args or {})
    sym, _note = resolve_symbol_arg(args, symbols, tool=name)
    if not sym:
        return symbol_error_payload(_note, symbols, tool=name)
    args["symbol"] = sym

    if name == "orderflow_tape":
        lim = max(1, min(int(args.get("limit") or 60), 500))
        rows = _q(bot_root,
                  "SELECT ts, buy_size, sell_size, delta, big_count, big_size, max_trade "
                  "FROM of_tape WHERE contract=? ORDER BY ts DESC LIMIT ?", (sym, lim))
        if rows and "error" in rows[0]:
            return {"symbol": sym, **rows[0]}
        rows.reverse()                       # 返回时按时间升序
        buy = sum(r["buy_size"] or 0 for r in rows)
        sell = sum(r["sell_size"] or 0 for r in rows)
        cvd = 0.0
        cvd_series = []
        for r in rows:
            cvd += r["delta"] or 0
            cvd_series.append(round(cvd, 2))
        big = sum(r["big_count"] or 0 for r in rows)
        return {
            "symbol": sym,
            "rows": len(rows),
            "window": {"from_ts": rows[0]["ts"] if rows else None,
                       "to_ts": rows[-1]["ts"] if rows else None},
            "totals": {"buy_size": round(buy, 2), "sell_size": round(sell, 2),
                       "delta": round(buy - sell, 2), "cvd": round(cvd, 2),
                       "big_count": big},
            "cvd_tail": cvd_series[-20:],
            "rows_detail": rows[-20:],
            "note": "taker 量由 WS 逐笔实测（size 符号即方向），非 K 线估算",
        }

    if name == "orderflow_footprint":
        minutes = max(1, min(int(args.get("minutes") or 15), 240))
        rows_n = max(3, min(int(args.get("rows") or 15), 60))
        import time as _t
        since = int(_t.time()) - minutes * 60
        rows = _q(bot_root,
                  "SELECT price, SUM(buy_size) AS buy, SUM(sell_size) AS sell "
                  "FROM of_footprint WHERE contract=? AND ts>=? GROUP BY price "
                  "ORDER BY (SUM(buy_size)+SUM(sell_size)) DESC LIMIT ?",
                  (sym, since, rows_n))
        if rows and "error" in rows[0]:
            return {"symbol": sym, **rows[0]}
        if not rows:
            return {"symbol": sym, "levels": [], "note": "无数据（采集未运行或窗口内无成交）"}
        levels = []
        imbalances = []
        for r in rows:
            b = r["buy"] or 0.0
            s = r["sell"] or 0.0
            tot = b + s
            ratio = (b / s) if s > 0 else (float("inf") if b > 0 else 1.0)
            levels.append({"price": r["price"], "buy": round(b, 2), "sell": round(s, 2),
                           "delta": round(b - s, 2),
                           "buy_ratio": None if s == 0 else round(b / s, 3)})
            # 业界判据：单档买卖比 >= 3:1 算失衡
            if s == 0 and b > 0:
                imbalances.append({"price": r["price"], "side": "buy", "ratio": None})
            elif ratio >= 3.0:
                imbalances.append({"price": r["price"], "side": "buy", "ratio": round(ratio, 2)})
            elif s > 0 and (s / b if b > 0 else float("inf")) >= 3.0:
                imbalances.append({"price": r["price"], "side": "sell",
                                   "ratio": round(s / b, 2) if b > 0 else None})
        poc = levels[0] if levels else None
        return {
            "symbol": sym,
            "window_minutes": minutes,
            "levels": levels,
            "poc": {"price": poc["price"], "total": round(poc["buy"] + poc["sell"], 2)} if poc else None,
            "imbalance_levels": imbalances,
            "imbalance_note": "判据：单档买卖比 >= 3:1；连续 >=3 档同向才算「失衡堆积」",
        }

    if name == "orderbook_state":
        lim = max(1, min(int(args.get("limit") or 60), 500))
        rows = _q(bot_root,
                  "SELECT ts, spread_pct, depth_bid, depth_ask, depth_ratio, "
                  "place_vol, cancel_vol, cancel_rate, traded_vol, intensity, grade, levels "
                  "FROM of_book_state WHERE contract=? ORDER BY ts DESC LIMIT ?", (sym, lim))
        if rows and "error" in rows[0]:
            return {"symbol": sym, **rows[0]}
        if not rows:
            return {"symbol": sym, "note": "无数据（采集未运行）"}
        latest = rows[0]
        grades = [r["grade"] for r in rows]
        return {
            "symbol": sym,
            "latest": latest,
            "grade_counts": {g: grades.count(g) for g in set(grades)},
            "series_tail": list(reversed(rows[:20])),
            "note": ("grade 为 poor/bad 时，微观信号应降权或跳过。"
                     "spread_pct 单位是百分比（0.0001 = 0.0001%）"),
        }

    if name == "orderbook_walls":
        min_age = float(args.get("min_age") or 30.0)
        lim = max(1, min(int(args.get("limit") or 60), 500))
        rows = _q(bot_root,
                  "SELECT ts, side, price, peak_size, age_sec, outcome FROM of_walls "
                  "WHERE contract=? AND age_sec>=? ORDER BY ts DESC LIMIT ?",
                  (sym, min_age, lim))
        if rows and "error" in rows[0]:
            return {"symbol": sym, **rows[0]}
        eaten = [r for r in rows if r.get("outcome") == "eaten"]
        cancelled = [r for r in rows if r.get("outcome") == "cancelled"]
        return {
            "symbol": sym,
            "min_age_sec": min_age,
            "walls": rows,
            "summary": {"total": len(rows), "eaten": len(eaten), "cancelled": len(cancelled)},
            "note": ("只有长寿挂单（age>=30s）才具参考价值；短命挂单视为诱导性。"
                     "eaten 表示被主动单吃掉，cancelled 表示被撤。"),
        }

    return {"error": f"unknown orderflow tool {name!r}",
            "allowed": list(ORDERFLOW_TOOL_NAMES)}
