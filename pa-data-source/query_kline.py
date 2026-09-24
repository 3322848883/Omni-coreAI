#!/usr/bin/env python3
"""query_kline.py — K线统一查询（本地库优先，交易所兜底）

用法:
    python query_kline.py -s BTC_USDT -i 15m -n 20           # 本地库查询（watchlist 品种）
    python query_kline.py -s DOGE_USDT -i 15m -n 50          # 非监控品种 → 自动转交易所拉取
    python query_kline.py -s BTC_USDT -i 15m --source local       # 强制本地（无数据则报错）
    python query_kline.py -s BTC_USDT -i 15m --source exchange    # 强制交易所（不落库）
    python query_kline.py -s BTC_USDT -i 15m --json          # JSON 输出（AI 用）
    python query_kline.py -s BTC_USDT -i 15m --env testnet   # 查模拟盘库（data/kline_testnet.db）
    python query_kline.py --schema                            # 查看库结构
    python query_kline.py --list-symbols                      # 列出本地库已监控品种

数据来源:
    local    — data/kline.db（watchlist 品种：2000根历史 + ema20/atr14 指标）
               --env testnet 时查 data/kline_testnet.db（模拟盘数据，与实盘隔离）
    exchange — Gate.io 公开 REST（任意合约，默认最新50根，上限1000，无指标，不落库）
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "kline.db")

# Gate.io 合约公开K线接口（无需密钥）
FUTURES_CANDLESTICKS_URL = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"

VALID_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
FIELDS = ["t", "o", "h", "l", "c", "v", "sum", "ema20", "atr14"]


def resolve_db_path(env):
    """按环境选择本地库：live → data/kline.db；testnet → data/kline_testnet.db（模拟盘隔离）"""
    if env == "testnet":
        return os.path.join(SCRIPT_DIR, "data", "kline_testnet.db")
    return os.path.join(SCRIPT_DIR, "data", "kline.db")


def fmt_time(ts):
    import time as _time
    return _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(ts + 8 * 3600))


def query_local(symbol, interval, limit):
    """查本地库；返回 (rows, meta)，无数据返回 (None, None)"""
    import sqlite3
    if not os.path.exists(DB_PATH):
        return None, None
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT t, o, h, l, c, v, sum, ema20, atr14 FROM kline "
            "WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?",
            (symbol, interval, limit)).fetchall()
        if not rows:
            return None, None
        rows = [list(r) for r in reversed(rows)]  # 时间升序
        is_testnet = "testnet" in DB_PATH
        meta = {"source": "local", "symbol": symbol, "interval": interval,
                "count": len(rows), "db": DB_PATH,
                "env": "testnet" if is_testnet else "live",
                "fields": FIELDS,
                "note": "本地库（watchlist 品种，含 ema20/atr14）" + ("；模拟盘数据" if is_testnet else "")}
        return rows, meta
    finally:
        conn.close()


def query_exchange(symbol, interval, limit):
    """Gate 公开接口拉取（任意合约，无需密钥，不落库）；返回 (rows, meta)"""
    url = "{}?contract={}&interval={}&limit={}".format(
        FUTURES_CANDLESTICKS_URL,
        urllib.parse.quote(symbol), interval, min(limit, 1000))
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list) or not data:
        return None, None
    # Gate 返回 [t, v, c, h, l, o, sum]；排序后转统一字段序
    parsed = []
    for item in data:
        if isinstance(item, dict):
            t = int(item.get("t", 0))
            o, h, l, c = item.get("o"), item.get("h"), item.get("l"), item.get("c")
            v, s = item.get("v"), item.get("sum")
        else:
            t = int(item[0])
            v, c, h, l, o, s = item[1], item[2], item[3], item[4], item[5], item[6]
        parsed.append([t, str(o), str(h), str(l), str(c), str(v), str(s), None, None])
    parsed.sort(key=lambda r: r[0])  # 时间升序
    parsed = parsed[-limit:]
    meta = {"source": "exchange", "symbol": symbol, "interval": interval,
            "count": len(parsed), "api": FUTURES_CANDLESTICKS_URL,
            "fields": FIELDS,
            "note": "交易所直拉（不落库，无 ema20/atr14）"}
    return parsed, meta


def list_local_symbols():
    import sqlite3
    if not os.path.exists(DB_PATH):
        print("本地库不存在:", DB_PATH)
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM kline ORDER BY symbol").fetchall()]
        print("本地库已监控品种:", ", ".join(symbols) if symbols else "(空)")
        return symbols
    finally:
        conn.close()


def show_schema():
    """库结构检查（原 query_kline.py 功能）"""
    import sqlite3
    if not os.path.exists(DB_PATH):
        print("本地库不存在:", DB_PATH)
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = conn.execute("PRAGMA table_info(kline)").fetchall()
        print("=== kline 表结构 ===")
        for c in cols:
            print("  {} ({})".format(c[1], c[2]))
        combos = conn.execute(
            "SELECT symbol, interval, COUNT(*), MIN(t), MAX(t) FROM kline "
            "GROUP BY symbol, interval ORDER BY symbol, interval").fetchall()
        print("\n=== 数据覆盖 ===")
        for sym, itv, cnt, tmin, tmax in combos:
            print("  {:12s} {:4s}  {:5d} rows  {} ~ {}".format(
                sym, itv, cnt, fmt_time(tmin), fmt_time(tmax)))
    finally:
        conn.close()


def main():
    global DB_PATH
    ap = argparse.ArgumentParser(
        description="K线统一查询：本地库优先，非监控品种自动转交易所")
    ap.add_argument("-s", "--symbol", help="合约名，如 BTC_USDT")
    ap.add_argument("-i", "--interval", default="15m",
                    choices=VALID_INTERVALS, help="K线周期（默认 15m）")
    ap.add_argument("-n", "--limit", type=int, default=50, help="返回根数（默认 50）")
    ap.add_argument("--env", choices=["live", "testnet"], default="live",
                    help="环境：live=实盘库（默认）/ testnet=模拟盘库 data/kline_testnet.db")
    ap.add_argument("--source", choices=["auto", "local", "exchange"],
                    default="auto",
                    help="auto=本地优先无则交易所（默认）；local=仅本地；exchange=仅交易所")
    ap.add_argument("--json", action="store_true", help="JSON 输出（AI 用）")
    ap.add_argument("--schema", action="store_true", help="查看库结构")
    ap.add_argument("--list-symbols", action="store_true", help="列出本地库已监控品种")
    args = ap.parse_args()

    DB_PATH = resolve_db_path(args.env)

    if args.schema:
        show_schema()
        return
    if args.list_symbols:
        list_local_symbols()
        return
    if not args.symbol:
        ap.error("需要 -s 指定合约，或使用 --schema / --list-symbols")

    symbol = args.symbol.upper()

    rows, meta, warn = None, None, None
    if args.source in ("auto", "local"):
        rows, meta = query_local(symbol, args.interval, args.limit)
    if rows is None and args.source in ("auto", "exchange"):
        if args.source == "auto":
            warn = "{} 不在本地库（非 watchlist 品种），已自动转交易所拉取".format(symbol)
        try:
            rows, meta = query_exchange(symbol, args.interval, args.limit)
        except Exception as e:
            if args.json:
                print(json.dumps({"error": str(e), "symbol": symbol}, ensure_ascii=False))
            else:
                print("交易所拉取失败: {}".format(e))
            sys.exit(1)

    if rows is None:
        msg = ("本地库无 {} {} 数据（未监控或已清空）".format(symbol, args.interval)
               if args.source == "local" else
               "本地库与交易所均无 {} 数据（合约名可能有误）".format(symbol))
        if args.json:
            print(json.dumps({"error": msg, "symbol": symbol}, ensure_ascii=False))
        else:
            print(msg)
            print("提示: 查看已监控品种 python query_kline.py --list-symbols")
        sys.exit(1)

    if args.json:
        out = dict(meta)
        out["warning"] = warn
        out["candles"] = [dict(zip(FIELDS, r)) for r in rows]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 人读格式
    if warn:
        print("⚠ {}".format(warn))
    print("[{}] {} {} — {} 根".format(
        meta["source"], symbol, args.interval, meta["count"]))
    print("{:<14s} {:>10s} {:>10s} {:>10s} {:>10s} {:>12s} {:>10s}".format(
        "时间", "open", "high", "low", "close", "volume", "ema20"))
    for r in rows:
        t, o, h, l, c, v, s, e, a = r
        print("{:<14s} {:>10s} {:>10s} {:>10s} {:>10s} {:>12s} {:>10s}".format(
            fmt_time(t), o, h, l, c, v, e if e else "-"))


if __name__ == "__main__":
    main()
