# -*- coding: utf-8 -*-
"""Market-data fetch/read test: REST live + local kline.db + hybrid snapshot + indicators.

Public REST needs no keys. Account block uses env keys (live or testnet).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.gate_client import GateApiError, GateClient  # noqa: E402
from gate_bot.strategist.indicators import attach_indicators, latest_indicators  # noqa: E402
from gate_bot.strategist.market import (  # noqa: E402
    MarketConfig,
    is_stale,
    load_local_candles,
    resolve_candles,
    resolve_db_path,
)
from gate_bot.strategist.snapshot import collect_snapshot  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    RESULTS.append((name, status, str(detail)[:110]))
    print("%s  %-36s %s" % (status, name, str(detail)[:110]))
    return ok


def main() -> int:
    # Prefer testnet keys when present; public market works on either host via env.
    env = "testnet" if os.environ.get("GATE_TESTNET_API_KEY") else "live"
    key = os.environ.get("GATE_TESTNET_API_KEY" if env == "testnet" else "GATE_API_KEY") or ""
    secret = os.environ.get("GATE_TESTNET_API_SECRET" if env == "testnet" else "GATE_API_SECRET") or ""
    c = GateClient(key, secret, env=env)
    symbols = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]
    print("ENV=%s  symbols=%s" % (env, symbols))
    print("=" * 72)

    # ── 1 public REST: last / tickers / candles ────────────
    print("\n[1] REST public market")
    lasts = {}
    for sym in symbols:
        try:
            px = c.get_last_price(sym)
            lasts[sym] = px
            rec("last_" + sym, px > 0, str(px))
        except Exception as e:  # noqa: BLE001
            rec("last_" + sym, False, str(e))

    try:
        raw = c.public_get("/api/v4/futures/usdt/tickers", "")
        n = len(raw or [])
        rec("tickers_list", n >= 3, "count=%d sample=%s" % (n, (raw[0] or {}).get("contract")))
    except Exception as e:  # noqa: BLE001
        rec("tickers_list", False, str(e))

    candle_counts = {}
    for sym in symbols:
        for interval in ("15m", "1h"):
            try:
                raw = c.public_get(
                    "/api/v4/futures/usdt/candlesticks",
                    "contract=%s&interval=%s&limit=60" % (sym, interval),
                )
                rows = raw or []
                candle_counts[(sym, interval)] = len(rows)
                rec("candles_%s_%s" % (sym, interval), len(rows) == 60, "n=%d t0=%s tN=%s" % (
                    len(rows),
                    rows[0][0] if rows and isinstance(rows[0], (list, tuple)) else (rows[0] or {}).get("t"),
                    rows[-1][0] if rows and isinstance(rows[-1], (list, tuple)) else (rows[-1] or {}).get("t"),
                ))
            except Exception as e:  # noqa: BLE001
                rec("candles_%s_%s" % (sym, interval), False, str(e))

    # ── 2 contract meta ────────────────────────────────────
    print("\n[2] contract meta")
    try:
        meta = c.get_contract("BTC_USDT")
        rec("contract_BTC", meta.quanto_multiplier > 0,
            "mult=%s pround=%s levmax=%s" % (meta.quanto_multiplier, meta.order_price_round, meta.leverage_max))
    except Exception as e:  # noqa: BLE001
        rec("contract_BTC", False, str(e))

    # ── 3 private account (live read) ──────────────────────
    print("\n[3] account REST")
    try:
        acc = c.get_account() or {}
        rec("account", "available" in acc or "total" in acc,
            "avail=%s total=%s mode=%s" % (acc.get("available"), acc.get("total"), acc.get("position_mode")))
    except Exception as e:  # noqa: BLE001
        rec("account", False, str(e))
    try:
        pos = c.get_positions() or []
        npos = sum(1 for p in pos if int(p.get("size") or 0) != 0)
        rec("positions", True, "rows=%d open=%d" % (len(pos), npos))
    except Exception as e:  # noqa: BLE001
        rec("positions", False, str(e))

    # ── 4 local kline.db (synthetic fixture + resolve path) ─
    print("\n[4] local kline.db")
    tmp = ROOT / ".market_read_test"
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # env: live → kline.db, testnet → kline_testnet.db (auto-switch in resolve_db_path)
    db_name = "kline_testnet.db" if env == "testnet" else "kline.db"
    db = data_dir / db_name
    if db.exists():
        db.unlink()
    now = int(time.time())
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE kline (t INTEGER, symbol TEXT, interval TEXT, o TEXT, h TEXT, l TEXT, c TEXT, "
        "v TEXT, sum TEXT, ema20 TEXT, atr14 TEXT)"
    )
    closes = []
    for i in range(60):
        t = now - (60 - i) * 900
        cl = 80000 + i * 10
        hi, lo = cl + 30, cl - 30
        closes.append(cl)
        # leave last bar ema20/atr14 NULL to exercise warm-start
        ema20 = None if i == 59 else str(cl)
        atr14 = None if i == 59 else "25.5"
        conn.execute(
            "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (t, "BTC_USDT", "15m", str(cl - 5), str(hi), str(lo), str(cl), "1.2", "0", ema20, atr14),
        )
    conn.commit()
    conn.close()

    cfg = MarketConfig(mode="hybrid", pa_data_root=str(data_dir), stale_factor=2.0)
    resolved = resolve_db_path(env, cfg, bot_root=tmp)
    rec("resolve_db_path", resolved == db, "want=%s got=%s" % (db, resolved))

    rows = load_local_candles(db, "BTC_USDT", "15m", 60)
    rec("local_candles_have_db_ema", rows is not None and rows[0].get("ema20") == closes[0],
        "row0 ema20=%s want=%s" % (rows[0].get("ema20") if rows else None, closes[0]))
    rec("load_local_candles", rows is not None and len(rows) == 60, "n=%s t_last=%s" % (
        len(rows) if rows else None, rows[-1]["t"] if rows else None))

    rec("is_stale_fresh", rows is not None and not is_stale(rows, "15m", 2.0), "fresh")
    stale_rows = [{"t": now - 100000}]
    rec("is_stale_old", is_stale(stale_rows, "15m", 2.0), "old bar")

    res = resolve_candles(c, "BTC_USDT", "15m", 50, market_cfg=cfg, env=env, bot_root=tmp)
    rec("hybrid_local_hit", res.source == "local" and not res.stale,
        "source=%s stale=%s n=%s degraded=%s" % (res.source, res.stale, len(res.rows), res.degraded))

    cfg_stale = MarketConfig(mode="hybrid", pa_data_root=str(data_dir), stale_factor=0.0001)
    res2 = resolve_candles(c, "BTC_USDT", "15m", 30, market_cfg=cfg_stale, env=env, bot_root=tmp)
    rec("hybrid_stale_rest_fallback", res2.source == "exchange",
        "source=%s degraded=%s" % (res2.source, res2.degraded))

    cfg_rest = MarketConfig(mode="rest_only")
    res3 = resolve_candles(c, "BTC_USDT", "15m", 20, market_cfg=cfg_rest, env=env, bot_root=tmp)
    rec("rest_only", res3.source == "exchange" and len(res3.rows) > 0, "n=%d" % len(res3.rows))

    cfg_local = MarketConfig(mode="local_only", pa_data_root=str(data_dir))
    res4 = resolve_candles(c, "BTC_USDT", "15m", 10, market_cfg=cfg_local, env=env, bot_root=tmp)
    rec("local_only_no_rest", res4.source == "local", "n=%d" % len(res4.rows))

    # ── 5 indicators on real + local rows ──────────────────
    print("\n[5] indicators")
    # use full 60-bar local series so row0 maps to closes[0]
    res_full = resolve_candles(c, "BTC_USDT", "15m", 60, market_cfg=cfg, env=env, bot_root=tmp)
    rows_ind = attach_indicators([dict(r) for r in (res_full.rows or [])], ["ema20", "ema50", "atr14", "rsi14"])
    last_ind = rows_ind[-1] if rows_ind else {}
    # last bar in fixture had NULL ema20 → warm-start from previous
    rec("ind_warm_start_ema20", last_ind.get("ema20") is not None, "ema20=%s" % last_ind.get("ema20"))
    rec("ind_ema50", last_ind.get("ema50") is not None, "ema50=%s" % last_ind.get("ema50"))
    rec("ind_atr14", last_ind.get("atr14") is not None, "atr14=%s" % last_ind.get("atr14"))
    rec("ind_rsi14", last_ind.get("rsi14") is not None, "rsi14=%s" % last_ind.get("rsi14"))
    # DB values kept for earlier bars
    rec("ind_keep_db_ema20", rows_ind[0].get("ema20") == closes[0],
        "row0 ema20=%s want=%s" % (rows_ind[0].get("ema20"), closes[0]))

    # REST candles + computed indicators
    rest_rows = attach_indicators([dict(r) for r in res3.rows], ["ema20", "ema50", "atr14", "rsi14"])
    rec("ind_rest_computed", rest_rows[-1].get("ema20") is not None and rest_rows[-1].get("rsi14") is not None,
        "ema20=%s rsi14=%s" % (rest_rows[-1].get("ema20"), rest_rows[-1].get("rsi14")))
    latest = latest_indicators(rest_rows)
    rec("latest_indicators", set(latest) == {"ema20", "ema50", "atr14", "rsi14"}, json.dumps(latest))

    # ── 6 collect_snapshot (rest_only + hybrid) ────────────
    print("\n[6] collect_snapshot")
    snap = collect_snapshot(c, symbols, candles=40, interval="15m",
                            market_cfg=MarketConfig(mode="rest_only"), env=env, bot_root=tmp)
    ok_meta = snap.get("meta", {}).get("market_mode") == "rest_only"
    ok_market = all(s in snap.get("market", {}) for s in symbols)
    ok_last = all("last" in snap["market"][s] for s in symbols)
    ok_ind = all("indicators" in snap["market"][s] for s in symbols)
    ok_acc = "error" not in (snap.get("account") or {})
    rec("snap_rest_only", ok_meta and ok_market and ok_last and ok_ind and ok_acc,
        "mode=%s keys=%s" % (snap.get("meta"), list(snap.get("market", {}).keys())))

    snap2 = collect_snapshot(c, ["BTC_USDT"], candles=30, interval="15m",
                             market_cfg=cfg, env=env, bot_root=tmp)
    rec("snap_hybrid_local", snap2["meta"]["candle_source"].get("BTC_USDT") == "local",
        "source=%s degraded=%s" % (snap2["meta"]["candle_source"], snap2["meta"]["degraded"]))
    rec("snap_hybrid_has_indicators", "indicators" in snap2["market"]["BTC_USDT"],
        json.dumps(snap2["market"]["BTC_USDT"].get("indicators")))

    # ── 7 multi-interval / multi-symbol consistency ────────
    print("\n[7] consistency")
    # candle timestamps strictly increasing
    for sym in symbols:
        raw = c.public_get("/api/v4/futures/usdt/candlesticks",
                           "contract=%s&interval=15m&limit=30" % sym)
        ts = [int(r[0]) if isinstance(r, (list, tuple)) else int(r.get("t")) for r in raw or []]
        rec("ts_sorted_%s" % sym, ts == sorted(ts) and len(set(ts)) == len(ts),
            "n=%d span=%ds" % (len(ts), (ts[-1] - ts[0]) if ts else -1))
    # OHLC sanity on one series
    raw = c.public_get("/api/v4/futures/usdt/candlesticks", "contract=BTC_USDT&interval=15m&limit=20")
    bad = 0
    for r in raw or []:
        if isinstance(r, (list, tuple)):
            o, h, l, cl = float(r[5]), float(r[3]), float(r[4]), float(r[2])
        else:
            o, h, l, cl = float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])
        if not (l <= min(o, cl) and max(o, cl) <= h and l <= h):
            bad += 1
    rec("ohlc_sanity_BTC", bad == 0, "bad=%d/20" % bad)

    # summary
    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 72)
    print("SUMMARY  PASS=%d  FAIL=%d  TOTAL=%d" % (n_pass, n_fail, len(RESULTS)))
    if n_fail:
        print("---- failures ----")
        for name, s, d in RESULTS:
            if s == "FAIL":
                print("  FAIL %s | %s" % (name, d))
    out = ROOT / "logs" / "market_read_test.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"env": env, "results": RESULTS, "lasts": lasts}, ensure_ascii=False) + "\n")
    print("log:", out)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
