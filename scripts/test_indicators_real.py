# -*- coding: utf-8 -*-
"""Real-market indicator validation (NO synthetic candles).

Fetches live/testnet Gate klines and independently recomputes EMA/SMA/BOLL
to prove package math. Then compares fill-path vs pure API.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import GateClient  # noqa: E402
from omnialpha.strategist.indicators import (  # noqa: E402
    atr,
    attach_indicators,
    boll,
    ema,
    latest_indicators,
    macd,
    rsi,
    sma,
)
from omnialpha.strategist.market import fetch_rest_candles  # noqa: E402
from omnialpha.strategist.triggers import evaluate_condition  # noqa: E402

RESULTS = []


def rec(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"
    RESULTS.append((name, st, str(detail)[:100]))
    print("%s  %-32s %s" % (st, name, str(detail)[:100]))
    return ok


def ref_ema(cs, n):
    k = 2.0 / (n + 1)
    s = sum(cs[:n]) / n
    out = [None] * (n - 1) + [s]
    for x in cs[n:]:
        s = x * k + s * (1 - k)
        out.append(s)
    return out


def ref_sma(cs, n):
    return [None if i + 1 < n else sum(cs[i - n + 1 : i + 1]) / n for i in range(len(cs))]


def ref_boll(cs, n=20, k=2.0):
    mid = ref_sma(cs, n)
    up, lo = [], []
    for i, m in enumerate(mid):
        if m is None:
            up.append(None)
            lo.append(None)
            continue
        w = cs[i - n + 1 : i + 1]
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        up.append(m + k * sd)
        lo.append(m - k * sd)
    return {"upper": up, "middle": mid, "lower": lo}


def main() -> int:
    env = "testnet" if os.environ.get("GATE_TESTNET_API_KEY") else "live"
    key = os.environ.get("GATE_TESTNET_API_KEY" if env == "testnet" else "GATE_API_KEY") or ""
    sec = os.environ.get("GATE_TESTNET_API_SECRET" if env == "testnet" else "GATE_API_SECRET") or ""
    c = GateClient(key, sec, env=env)
    print("ENV", env, "symbol=BTC_USDT interval=15m")
    rows = fetch_rest_candles(c, "BTC_USDT", "15m", 200)
    rec("fetch_real_candles", len(rows) >= 150, "n=%d last=%s t=%s" % (len(rows), rows[-1]["c"], rows[-1]["t"]))
    closes = [r["c"] for r in rows]
    highs = [r["h"] for r in rows]
    lows = [r["l"] for r in rows]

    e9a, e9b = ema(closes, 9), ref_ema(closes, 9)
    rec("ema9_vs_independent", all((a is None and b is None) or abs(a - b) < 1e-9 for a, b in zip(e9a, e9b)),
        "last=%.4f" % e9a[-1])
    s7a, s7b = sma(closes, 7), ref_sma(closes, 7)
    rec("sma7_vs_independent", all((a is None and b is None) or abs(a - b) < 1e-9 for a, b in zip(s7a, s7b)),
        "last=%.4f" % s7a[-1])
    bs, rb = boll(closes, 20, 2.0), ref_boll(closes, 20, 2.0)
    rec("boll20_vs_independent",
        abs(bs["middle"][-1] - rb["middle"][-1]) < 1e-9 and abs(bs["upper"][-1] - rb["upper"][-1]) < 1e-9,
        "up=%.2f mid=%.2f lo=%.2f" % (bs["upper"][-1], bs["middle"][-1], bs["lower"][-1]))

    out = attach_indicators(
        [dict(r) for r in rows], ["ema9", "sma20", "rsi14", "atr14", "macd", "macd_dea", "macd_hist", "boll20"]
    )
    m = macd(closes, 12, 26, 9)
    rec("fill_vs_api_ema9", abs(out[-1]["ema9"] - ema(closes, 9)[-1]) < 1e-6, "")
    rec("fill_vs_api_sma20", abs(out[-1]["sma20"] - sma(closes, 20)[-1]) < 1e-6, "")
    rec("fill_vs_api_rsi14", abs(out[-1]["rsi14"] - rsi(closes, 14)[-1]) < 1e-6, "rsi=%.2f" % out[-1]["rsi14"])
    rec("fill_vs_api_atr14", abs(out[-1]["atr14"] - atr(highs, lows, closes, 14)[-1]) < 1e-6, "")
    rec("fill_vs_api_macd", abs(out[-1]["macd"] - m["dif"][-1]) < 1e-6 and abs(out[-1]["macd_dea"] - m["dea"][-1]) < 1e-6,
        "dif=%.4f dea=%.4f" % (out[-1]["macd"], out[-1]["macd_dea"]))
    rec("fill_vs_api_boll", abs(out[-1]["boll_upper"] - bs["upper"][-1]) < 1e-6, "")

    r14 = out[-1]["rsi14"]
    rec("rsi_range", r14 is not None and 0 <= r14 <= 100, "rsi14=%.2f" % r14)
    rec("boll_order", bs["lower"][-1] < bs["middle"][-1] < bs["upper"][-1], "")
    rec("hist_identity", abs(out[-1]["macd_hist"] - (m["dif"][-1] - m["dea"][-1])) < 1e-6, "")

    print("LATEST", {k: (round(v, 4) if isinstance(v, float) else v)
                     for k, v in latest_indicators(out, ["ema9", "sma20", "rsi14", "atr14", "macd", "boll20"]).items()})

    # triggers on same real series via evaluate_condition (also real REST)
    for cond in (
        {"type": "rsi", "symbol": "BTC_USDT", "period": 14, "op": "lt", "level": 80},
        {"type": "macd_cross", "symbol": "BTC_USDT", "dir": "any"},
        {"type": "boll_break", "symbol": "BTC_USDT", "side": "upper"},
    ):
        ok, reason = evaluate_condition(c, cond, "15m")
        rec("cond_" + cond["type"], isinstance(ok, bool) and bool(reason), reason[:60])

    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("\nSUMMARY PASS=%d FAIL=%d TOTAL=%d" % (n_pass, n_fail, len(RESULTS)))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
