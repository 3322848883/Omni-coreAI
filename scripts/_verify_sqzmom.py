"""临时：实盘验证 SQZMOM。"""
from gate_bot.exchanges.registry import create_exchange
from gate_bot.strategist.indicators import attach_indicators, latest_indicators


def main() -> None:
    ex = create_exchange("gate", env="live")
    rows = ex.get_klines("BTC_USDT", "15m", 250)
    wanted = ["sqzmom", "sqzmom_state", "sqzmom14_14", "sqz_on", "momentum_up"]
    attach_indicators(rows, wanted)
    last = latest_indicators(rows, wanted)
    print("n =", len(rows))
    for k, v in last.items():
        print(f"{k:<16} {v}")
    print("--- last 8 bars ---")
    for r in rows[-8:]:
        print(
            f"  t={r['t']} c={r['c']:.1f} mom={r.get('sqzmom')} "
            f"state={r.get('sqz_state')} on={r.get('sqz_on')}"
        )


if __name__ == "__main__":
    main()
