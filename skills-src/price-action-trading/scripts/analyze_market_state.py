#!/usr/bin/env python3
"""
市场状态快速扫描脚本
用法：python analyze_market_state.py [symbol] [timeframes]
示例：python analyze_market_state.py btc 4h,1h,5m

环境变量：
  PRICE_ACTION_DATA_DIR  K线 JSON 目录（默认 <skill>/data）
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

SKILL_DIR = Path(os.environ.get(
    "PRICE_ACTION_SKILL_ROOT",
    Path(__file__).resolve().parents[1],
))
DATA_DIR = Path(os.environ.get(
    "PRICE_ACTION_DATA_DIR",
    SKILL_DIR / "data",
))


def load_kline_data(symbol, timeframe, limit=200):
    file_path = DATA_DIR / f"{symbol}_usdt_{timeframe}.json"
    if not file_path.exists():
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data[-limit:] if len(data) > limit else data


def analyze_timeframe(bars, timeframe):
    if not bars:
        return {"error": f"无数据（DATA_DIR={DATA_DIR}）"}

    latest = bars[-1]
    close = float(latest["c"])
    ema20 = float(latest["ema20"]) if latest.get("ema20") else None
    atr14 = float(latest["atr14"]) if latest.get("atr14") else None

    if not ema20:
        return {"error": "EMA20 数据不足"}

    valid_bars = [b for b in bars[-20:] if b.get("ema20")]
    above_ema = sum(1 for b in valid_bars if float(b["c"]) > float(b["ema20"]))
    ratio = above_ema / len(valid_bars) if valid_bars else 0

    if ratio > 0.6:
        ai_state = "AIL"
    elif ratio < 0.4:
        ai_state = "AIS"
    else:
        ai_state = "不确定"

    highs = [float(b["h"]) for b in bars[-20:]]
    lows = [float(b["l"]) for b in bars[-20:]]
    hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i - 1])
    ll = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i - 1])
    day_range = max(highs) - min(lows) if highs and lows else None
    range_pct = (day_range / close * 100) if day_range and close else None

    return {
        "timeframe": timeframe,
        "close": close,
        "ema20": ema20,
        "atr14": atr14,
        "ai_state": ai_state,
        "above_ema": f"{above_ema}/{len(valid_bars)}",
        "ema_ratio": round(ratio, 3),
        "hh_ll": f"HH={hh}, LL={ll}",
        "range_20": round(day_range, 6) if day_range else None,
        "range_pct": round(range_pct, 2) if range_pct else None,
        "bars": len(bars),
        "ts_utc": datetime.now(timezone.utc).isoformat(),
    }


def main():
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "btc").lower()
    tfs = (sys.argv[2] if len(sys.argv) > 2 else "4h,1h,5m").split(",")
    print(f"DATA_DIR = {DATA_DIR}")
    if not DATA_DIR.exists():
        print("ERROR: 数据目录不存在。请设置 PRICE_ACTION_DATA_DIR。")
        sys.exit(1)
    for tf in tfs:
        tf = tf.strip()
        if not tf:
            continue
        bars = load_kline_data(symbol, tf)
        result = analyze_timeframe(bars, tf)
        print(f"\n=== {symbol.upper()} {tf.upper()} ===")
        for k, v in result.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
