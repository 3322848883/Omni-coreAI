"""临时：实测六所 get_klines / smc 取数是否各自独立。"""
from __future__ import annotations

import time

from omnialpha.exchanges import list_exchanges
from omnialpha.exchanges.registry import create_exchange
from omnialpha.strategist.market import MarketConfig
from omnialpha.strategist.tools import run_tool


def main() -> None:
    now = int(time.time())
    print(f"{'venue':<12} {'n':>4} {'last_t_delta':>12} {'last_c':>12} {'source':<8} {'swing':<8} {'zone':<10}")
    for name in list_exchanges():
        try:
            ex = create_exchange(name, env="live")
            rows = ex.get_klines("BTC_USDT", "15m", 30)
            n = len(rows or [])
            last = (rows or [{}])[-1]
            t_delta = now - int(last.get("t") or 0)
            out = run_tool(
                ex, "smc",
                {"symbol": "BTC_USDT", "tf": "15m", "limit": 80},
                env="live",
                market_cfg=MarketConfig(mode="rest_only", exchange=name),
            )
            swing = out.get("swing_trend", "-")
            zone = (out.get("premium_discount") or {}).get("current_zone", "-")
            src = out.get("source", "-")
            err = out.get("error")
            if err:
                print(f"{name:<12} ERR {err}")
                continue
            print(
                f"{name:<12} {n:>4} {t_delta:>12} {last.get('c'):>12} {src:<8} {swing:<8} {zone:<10}"
            )
            # 各所最后一根收盘应不同（至少不全等），若完全一致说明串了数据源
        except Exception as e:  # noqa: BLE001
            print(f"{name:<12} FAIL {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
