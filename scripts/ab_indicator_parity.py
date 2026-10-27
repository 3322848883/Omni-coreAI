#!/usr/bin/env python3
"""P0 验收工具：库内已存指标 vs 新引擎重算（真实数据 A/B 对拍）。

用法：
    python scripts/ab_indicator_parity.py --db /path/kline.db [--limit 2000] [--top 5]

判据（方案 §9.3）：同算法应 ≤1e-9 相对误差；打印每序列的 max/top-N 偏差。
只读，不写库。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pa-data-source"))
sys.path.insert(0, str(ROOT / "scripts"))

import indicators_core as core  # noqa: E402
from _kline_read import read_candles, series_list  # noqa: E402


def _rel(a: float, b: float) -> float:
    if a == b:
        return 0.0
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom


def compare_series(db: Path, symbol: str, interval: str, limit: int, top: int) -> dict:
    rows = read_candles(db, symbol, interval, limit=limit)
    if len(rows) < 30:
        return {"symbol": symbol, "interval": interval, "rows": len(rows), "skip": "too_short"}
    c = [r["c"] for r in rows]
    h = [r["h"] for r in rows]
    l = [r["l"] for r in rows]

    checks = {
        "ema20": (core.ema(c, 20), "ema20"),
        "atr14": (core.atr(h, l, c, 14), "atr14"),
        "rsi14": (core.rsi(c, 14), "rsi14"),
    }
    macd = core.macd(c, 12, 26, 9)
    checks["macd"] = (macd["dif"], "macd")
    checks["macd_dea"] = (macd["dea"], "macd_dea")
    checks["macd_hist"] = (macd["hist"], "macd_hist")
    boll = core.boll(c, 20, 2.0)
    checks["boll20"] = (boll["middle"], "boll20")
    checks["boll20_upper"] = (boll["upper"], "boll_upper")
    checks["boll20_lower"] = (boll["lower"], "boll_lower")
    checks["ema50"] = (core.ema(c, 50), "ema50")
    checks["ema200"] = (core.ema(c, 200), "ema200")

    detail = {}
    for label, (series, key) in checks.items():
        pairs = [(i, series[i], r.get(key)) for i, r in enumerate(rows)
                 if series[i] is not None and r.get(key) is not None]
        if not pairs:
            detail[label] = {"n": 0}
            continue
        worst = max(pairs, key=lambda p: _rel(p[1], p[2]))
        detail[label] = {
            "n": len(pairs),
            "max_rel": _rel(worst[1], worst[2]),
            "worst_idx": worst[0],
            "ours": worst[1],
            "stored": worst[2],
        }
    return {"symbol": symbol, "interval": interval, "rows": len(rows), "detail": detail}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "pa-data-source/data/kline.db"))
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--top", type=int, default=0, help="只测前 N 个序列")
    args = ap.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"[FAIL] db missing: {db}")
        return 2

    series = series_list(db)
    if args.top:
        series = series[: args.top]
    print(f"db={db}  series={len(series)}  limit={args.limit}\n")
    bad = 0
    for symbol, interval, n, t0, t1 in series:
        res = compare_series(db, symbol, interval, args.limit, args.top)
        if res.get("skip"):
            print(f"{symbol:10s} {interval:>4s} rows={res['rows']:<5d} SKIP({res['skip']})")
            continue
        worst_label, worst_rel = None, -1.0
        for label, d in res["detail"].items():
            if d.get("n") and d["max_rel"] > worst_rel:
                worst_label, worst_rel = label, d["max_rel"]
        flag = "OK " if worst_rel <= 1e-9 else "FAIL"
        if worst_rel > 1e-9:
            bad += 1
        print(f"{symbol:10s} {interval:>4s} rows={res['rows']:<5d} "
              f"worst={worst_label:<12s} max_rel={worst_rel:.3e}  {flag}")
        if worst_rel > 1e-9:
            print("        ", res["detail"][worst_label])
    print(f"\n序列偏差 >1e-9 的数量：{bad}/{len(series)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
