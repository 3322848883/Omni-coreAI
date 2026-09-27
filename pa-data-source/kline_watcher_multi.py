"""多所 K 线采集：为每个交易所写 kline_<ex>.db，schema 与 Gate kline.db 完全一致。

- 契约：contracts/KLINE_SCHEMA.md（t/symbol/interval/o,h,l,c,v,sum,ema20,atr14）
- 库文件：data/kline_<ex>.db（live） / data/kline_<ex>_testnet.db（testnet）
- 取数：gate_bot.exchanges 各所公开 REST get_klines（与 bot 工具同一数据源）
- 实时：ws_venues 公开 WS candle 推送（对齐 Gate 模式：WS 主通道 + REST 兜底/补全）
- 指标：compute_ema / compute_atr 与 kline_watcher.py 同算法

用法：
  python kline_watcher_multi.py                         # 全部非 Gate 所，WS 优先 + REST 兜底
  python kline_watcher_multi.py --mode rest             # 强制 REST 轮询
  python kline_watcher_multi.py --mode ws               # 强制 WS（连不上则重试）
  python kline_watcher_multi.py --exchanges binance,okx
  python kline_watcher_multi.py --env testnet
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
import time
import traceback
from typing import Any, Callable, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
# monorepo root（worktree 或主仓）
ROOT = os.path.dirname(SCRIPT_DIR)

# 默认非 Gate 所（Gate 由 kline_watcher.py 采集，不重复写 kline.db）
DEFAULT_EXCHANGES = ["binance", "okx", "bybit", "bitget", "hyperliquid"]
DEFAULT_SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "XRP_USDT"]
DEFAULT_INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"]
POLL_SEC = 30
MAX_CANDLES = 2000
# WS 断线重连退避
WS_RETRY_SEC = 20


# ── 指标（与 kline_watcher.compute_ema / compute_atr 同算法）────────────
def compute_ema(data: list[dict], period: int = 20) -> None:
    if len(data) < period:
        return
    k = 2.0 / (period + 1)
    sma = sum(float(d["c"]) for d in data[:period]) / period
    for i in range(period - 1):
        data[i]["ema20"] = None
    data[period - 1]["ema20"] = str(round(sma, 8))
    for i in range(period, len(data)):
        prev_ema = float(data[i - 1]["ema20"])
        close_val = float(data[i]["c"])
        ema_val = close_val * k + prev_ema * (1 - k)
        data[i]["ema20"] = str(round(ema_val, 8))


def compute_atr(data: list[dict], period: int = 14) -> None:
    if len(data) < 2:
        for d in data:
            d["atr14"] = None
        return
    data[0]["atr14"] = None
    trs = []
    for i in range(1, len(data)):
        h = float(data[i]["h"])
        l = float(data[i]["l"])
        pc = float(data[i - 1]["c"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        if i < period:
            data[i]["atr14"] = None
        elif i == period:
            atr_val = sum(trs) / period
            data[i]["atr14"] = str(round(atr_val, 8))
        else:
            prev_atr = float(data[i - 1]["atr14"])
            atr_val = (prev_atr * (period - 1) + tr) / period
            data[i]["atr14"] = str(round(atr_val, 8))


# ── 库（与 Gate kline.db 同 schema）────────────────────────────────────
KLINE_DDL = """
CREATE TABLE IF NOT EXISTS [kline] (
    t INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    o TEXT NOT NULL, h TEXT NOT NULL, l TEXT NOT NULL, c TEXT NOT NULL,
    v TEXT NOT NULL, sum TEXT NOT NULL,
    ema20 TEXT, atr14 TEXT,
    PRIMARY KEY (symbol, interval, t)
)
"""


def db_path_for(exchange: str, env: str = "live") -> str:
    name = f"kline_{exchange}.db"
    if str(env).lower() == "testnet":
        name = f"kline_{exchange}_testnet.db"
    return os.path.join(DATA_DIR, name)


def open_db(path: str) -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(KLINE_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_time ON [kline] (t)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_symbol ON [kline] (symbol, t)")
    conn.commit()
    return conn


def load_sorted(conn: sqlite3.Connection, symbol: str, interval: str) -> list[dict]:
    cur = conn.execute(
        "SELECT t,o,h,l,c,v,sum,ema20,atr14 FROM [kline] WHERE symbol=? AND interval=? ORDER BY t ASC",
        (symbol, interval),
    )
    return [
        {"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5], "sum": r[6], "ema20": r[7], "atr14": r[8]}
        for r in cur.fetchall()
    ]


def upsert_rows(conn: sqlite3.Connection, symbol: str, interval: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO [kline] (symbol,interval,t,o,h,l,c,v,sum,ema20,atr14) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                symbol, interval, int(r["t"]),
                str(r["o"]), str(r["h"]), str(r["l"]), str(r["c"]),
                str(r["v"]), str(r.get("sum") or 0),
                r.get("ema20"), r.get("atr14"),
            )
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def truncate(conn: sqlite3.Connection, symbol: str, interval: str, max_candles: int = MAX_CANDLES) -> None:
    if max_candles <= 0:
        return
    conn.execute(
        """DELETE FROM [kline] WHERE symbol=? AND interval=? AND t < (
               SELECT MIN(t) FROM (
                   SELECT t FROM [kline] WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?
               )
           )""",
        (symbol, interval, symbol, interval, int(max_candles)),
    )
    conn.commit()


def merge_and_write(conn: sqlite3.Connection, symbol: str, interval: str, fetched: list[dict]) -> int:
    """合并新拉取的 bar → 重算 ema20/atr14 → 写库。"""
    if not fetched:
        return 0
    existing = {int(r["t"]): r for r in load_sorted(conn, symbol, interval)}
    for r in fetched:
        existing[int(r["t"])] = {
            "t": int(r["t"]),
            "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
            "v": r.get("v") or 0, "sum": r.get("sum") or 0,
            "ema20": None, "atr14": None,
        }
    merged = [existing[t] for t in sorted(existing)]
    compute_ema(merged)
    compute_atr(merged)
    n = upsert_rows(conn, symbol, interval, merged)
    truncate(conn, symbol, interval)
    return n


# ── 取数（gate_bot.exchanges，与 bot 工具同一数据源）─────────────────
def _import_create_exchange():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from gate_bot.exchanges.registry import create_exchange  # noqa: PLC0415
    except ImportError as e:
        raise SystemExit(
            f"需要 monorepo 中的 gate_bot.exchanges（ROOT={ROOT}）。\n"
            f"请从 gate-signal-bot 仓库内运行本脚本，或设 PYTHONPATH 指向仓库根。原始错误: {e}"
        ) from e
    return create_exchange


def fetch_candles(client, symbol: str, interval: str, limit: int) -> list[dict]:
    rows = client.get_klines(symbol, interval, limit) or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "t": int(r.get("t") or 0),
                "o": r.get("o"),
                "h": r.get("h"),
                "l": r.get("l"),
                "c": r.get("c"),
                "v": r.get("v") or 0,
                "sum": r.get("sum") or 0,
            }
        )
    out.sort(key=lambda x: x["t"])
    return out


def collect_once(
    exchange: str,
    env: str,
    symbols: list[str],
    intervals: list[str],
    limit: int = 200,
    verbose: bool = True,
) -> dict:
    create_exchange = _import_create_exchange()
    client = create_exchange(exchange, env=env)
    path = db_path_for(exchange, env)
    conn = open_db(path)
    stats = {"exchange": exchange, "db": os.path.basename(path), "ok": 0, "fail": 0, "bars": 0, "errors": []}
    try:
        for sym in symbols:
            for iv in intervals:
                try:
                    rows = fetch_candles(client, sym, iv, limit)
                    n = merge_and_write(conn, sym, iv, rows)
                    stats["ok"] += 1
                    stats["bars"] += n
                    if verbose:
                        print(f"  [{exchange}] {sym} {iv}: {len(rows)} bars fetched, db total upsert {n}")
                except Exception as e:  # noqa: BLE001
                    stats["fail"] += 1
                    stats["errors"].append(f"{sym}/{iv}: {type(e).__name__}: {e}")
                    if verbose:
                        print(f"  [{exchange}] {sym} {iv} FAIL {type(e).__name__}: {e}")
    finally:
        conn.close()
    return stats


# ── WS 实时通道（对齐 Gate：WS 主 + REST 兜底/补全）─────────────────
class VenueFeed:
    """一个交易所：WS 实时写库；WS 不可用/断线时 REST 轮询兜底。"""

    def __init__(self, exchange: str, env: str, symbols: list[str], intervals: list[str],
                 mode: str = "hybrid", limit: int = 200, rest_poll: float = 30.0):
        self.exchange = exchange
        self.env = env
        self.symbols = symbols
        self.intervals = intervals
        self.mode = mode  # hybrid | ws | rest
        self.limit = limit
        self.rest_poll = rest_poll
        self.path = db_path_for(exchange, env)
        self._conn: Optional[sqlite3.Connection] = None
        self._conn_lock = threading.Lock()
        self._ws_mode = False  # 当前是否 WS 主通道
        self._ws_fail_until = 0.0  # WS 失败退避截止时刻
        self._stop = threading.Event()
        self.last_rest_ok = ""
        self.last_ws_ok = ""
        self.stats = {"ws_msgs": 0, "ws_candles": 0, "rest_rounds": 0, "rest_errors": 0, "mode": "init"}

    def _db(self) -> sqlite3.Connection:
        with self._conn_lock:
            if self._conn is None:
                self._conn = open_db(self.path)
            return self._conn

    def log(self, s: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} [{self.exchange}] {s}", flush=True)

    # — REST 兜底 / 补全 —
    def rest_round(self, verbose: bool = False) -> None:
        try:
            st = collect_once(self.exchange, self.env, self.symbols, self.intervals,
                              limit=self.limit, verbose=verbose)
            self.stats["rest_rounds"] += 1
            self.last_rest_ok = time.strftime("%H:%M:%S")
            if st["fail"]:
                self.stats["rest_errors"] += 1
                self.log(f"REST partial fail={st['fail']} {st['errors'][:2]}")
        except Exception as e:  # noqa: BLE001
            self.stats["rest_errors"] += 1
            self.log(f"REST FAIL {type(e).__name__}: {e}")

    def on_candles(self, symbol: str, interval: str, rows: list[dict]) -> None:
        if not rows:
            return
        iv = interval or self.intervals[0]
        try:
            conn = self._db()
            with self._conn_lock:
                merge_and_write(conn, symbol, iv, rows)
            self.stats["ws_candles"] += len(rows)
            self.last_ws_ok = time.strftime("%H:%M:%S")
        except Exception as e:  # noqa: BLE001
            self.log(f"WS write FAIL {type(e).__name__}: {e}")

    def on_status(self, s: str) -> None:
        self.log(s)

    def _ws_thread(self) -> None:
        try:
            from ws_venues import WSConsumer, get_venue
        except ImportError as e:
            self.log(f"ws_venues unavailable ({e}) → REST only")
            self._ws_mode = False
            self._ws_fail_until = time.time() + 3600  # 不再重试
            return
        try:
            venue = get_venue(self.exchange)
        except Exception as e:  # noqa: BLE001
            self.log(f"no WS venue {self.exchange}: {e}")
            self._ws_fail_until = time.time() + 3600
            return
        subs = [(s, iv) for s in self.symbols for iv in self.intervals]
        self._ws_mode = True
        self.log(f"WS start {venue.url} subs={len(subs)}")
        # 连不上时 WebSocketApp.run_forever 会抛错/返回，由外层退避重试
        try:
            consumer = WSConsumer(venue, subs, self.on_candles, self.on_status)
            consumer.run_forever()
        except Exception as e:  # noqa: BLE001
            self.log(f"WS thread end {type(e).__name__}: {e}")
        finally:
            self._ws_mode = False
            # 断线/失败 → 退避 WS_RETRY_SEC 再试，期间走 REST
            self._ws_fail_until = time.time() + WS_RETRY_SEC

    def run(self) -> None:
        """hybrid: WS 优先，失败/断线回 REST；ws: 只 WS 重试；rest: 只轮询。"""
        self.log(f"feed start mode={self.mode} db={os.path.basename(self.path)} "
                 f"symbols={self.symbols} intervals={self.intervals}")
        ws_thread: Optional[threading.Thread] = None
        last_rest = 0.0
        while not self._stop.is_set():
            now = time.time()
            # 启/重连 WS（失败退避 WS_RETRY_SEC）
            if self.mode in ("hybrid", "ws") and (ws_thread is None or not ws_thread.is_alive()):
                if now >= self._ws_fail_until:
                    ws_thread = threading.Thread(target=self._ws_thread, daemon=True)
                    ws_thread.start()
            # REST：rest 模式恒跑；hybrid 在 WS 未就绪时兜底；ws 模式连不上也兑底
            need_rest = False
            if self.mode == "rest":
                need_rest = True
            elif self.mode == "hybrid":
                need_rest = (not self._ws_mode) or (now - last_rest >= max(60.0, self.rest_poll * 2))
            elif self.mode == "ws" and not self._ws_mode:
                need_rest = True
            if need_rest and (now - last_rest) >= self.rest_poll:
                self.rest_round()
                last_rest = now
            self.stats["mode"] = "ws" if self._ws_mode else "rest"
            time.sleep(1.0)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()


def run_loop(
    exchanges: list[str],
    env: str,
    symbols: list[str],
    intervals: list[str],
    poll: float,
    limit: int,
    mode: str = "hybrid",
) -> None:
    print(
        f"kline_watcher_multi start: mode={mode} exchanges={exchanges} env={env} "
        f"symbols={symbols} intervals={intervals} poll={poll}s → {DATA_DIR}"
    )
    feeds = [
        VenueFeed(ex, env, symbols, intervals, mode=mode, limit=limit, rest_poll=poll)
        for ex in exchanges
    ]
    threads = [threading.Thread(target=f.run, daemon=True) for f in feeds]
    for th in threads:
        th.start()
    try:
        while True:
            time.sleep(15)
            summary = " | ".join(
                f"{f.exchange}:{f.stats['mode']}"
                f"/ws_c={f.stats['ws_candles']}"
                f"/rest={f.stats['rest_rounds']}"
                f"/err={f.stats['rest_errors']}"
                for f in feeds
            )
            print(f"{time.strftime('%H:%M:%S')} {summary}", flush=True)
    except KeyboardInterrupt:
        print("stopping...")
        for f in feeds:
            f.stop()
        time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="多所 K 线采集（写 kline_<ex>.db，schema 与 Gate 一致）")
    ap.add_argument("--exchanges", default=",".join(DEFAULT_EXCHANGES), help="逗号分隔，默认非 Gate 全部")
    ap.add_argument("--exchange", default="", help="单所（覆盖 --exchanges）")
    ap.add_argument("--env", default="live", choices=["live", "testnet"])
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    ap.add_argument("--poll", type=float, default=POLL_SEC, help="REST 轮询间隔秒（默认 30）")
    ap.add_argument("--limit", type=int, default=200, help="每次拉取 bar 数（默认 200）")
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "ws", "rest"],
                    help="hybrid=WS优先+REST兜底（默认）| ws=仅WS | rest=仅REST轮询")
    ap.add_argument("--once", action="store_true", help="只采一轮 REST 退出（不启 WS）")
    args = ap.parse_args()

    exchanges = (
        [args.exchange.strip().lower()]
        if args.exchange.strip()
        else [x.strip().lower() for x in args.exchanges.split(",") if x.strip()]
    )
    exchanges = [e for e in exchanges if e and e != "gate"]
    if not exchanges:
        print("no exchanges to collect (gate is handled by kline_watcher.py)")
        return 1
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    intervals = [i.strip().lower() for i in args.intervals.split(",") if i.strip()]
    # 周期白名单校验（防止命令行截断如 1d→1 写入脏数据）
    valid_tf = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    bad = [i for i in intervals if i not in valid_tf]
    if bad:
        print(f"非法周期 {bad}（合法: {sorted(valid_tf)}），已忽略")
        intervals = [i for i in intervals if i in valid_tf]
    if not intervals:
        print("无合法周期，退出")
        return 1

    if args.once:
        total = {"ok": 0, "fail": 0, "bars": 0}
        for ex in exchanges:
            st = collect_once(ex, args.env, symbols, intervals, limit=args.limit, verbose=True)
            total["ok"] += st["ok"]
            total["fail"] += st["fail"]
            total["bars"] += st["bars"]
        print(f"done: ok={total['ok']} fail={total['fail']} bars={total['bars']}")
        return 0 if total["fail"] == 0 else 2

    try:
        run_loop(exchanges, args.env, symbols, intervals, args.poll, args.limit, mode=args.mode)
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
