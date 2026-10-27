#!/usr/bin/env python3
"""实测：指标工具能拉哪些周期？直接打 Gate 原生端点 + 走 fetch_rest_candles 解析。"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.market import fetch_rest_candles  # noqa: E402
from omnialpha.strategist.indicators import attach_indicators, latest_indicators  # noqa: E402


class PublicOnlyClient:
    """无鉴权的行情 client：public_get 直连 Gate（与 bot 的 REST 路径同解析）。"""

    def public_get(self, path, query_string="", **kw):
        url = f"https://api.gateio.ws{path}?{query_string}"
        return json.loads(urllib.request.urlopen(url, timeout=20).read())


c = PublicOnlyClient()
names = ["ema20", "boll20", "atr14"]
print(f"{'tf':>5s} {'bars':>5s} {'ema20':>10s} {'boll20':>10s} {'atr14':>9s}  结果")
for tf in ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "7d", "30d", "10s", "3m"):
    try:
        rows = fetch_rest_candles(c, "ETH_USDT", tf, 300)
        if not rows:
            print(f"{tf:>5s} {'0':>5s}  —  空")
            continue
        out = latest_indicators(attach_indicators(list(rows), names), names)
        print(f"{tf:>5s} {len(rows):>5d} {str(out['ema20'])[:9]:>10s} "
              f"{str(out['boll20'])[:9]:>10s} {str(out['atr14'])[:8]:>9s}  OK")
    except Exception as e:  # noqa: BLE001
        print(f"{tf:>5s}   ERR  {type(e).__name__}: {str(e)[:70]}")
