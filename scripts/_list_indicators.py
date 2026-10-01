"""列当前可解析的指标族与代表名。"""
from gate_bot.strategist.indicators import IndicatorNameError, parse_indicator_name

CANDIDATES = [
    # 均线
    "ema20", "sma20", "ma7", "rma14", "wma20", "vwma20", "hma20",
    "kama10", "alma20", "t3", "lsma20", "linreg20",
    # 波动/通道
    "atr14", "atr14_ema", "atr14_sma", "atr14_wma",
    "boll20", "boll20_2", "boll_upper_band", "boll_sma_upper_band",
    "linreg_channel20",
    # 震荡
    "rsi14", "rsi14_ema", "rsi14_bb", "stoch14", "stoch14_d",
    "cci20", "wr14", "mfi14", "adx14", "macd", "macd_dea", "macd_hist",
    "macd12_26_9",
    # 量能
    "vwap", "obv",
    # 趋势
    "supertrend", "supertrend10",
    # 挤压
    "sqzmom", "sqz_mom", "sqz_state",
    # Pine 套件
    "ema_smooth", "ema_boll", "pine_ema",
]

kinds: dict[str, list[str]] = {}
fails: list[str] = []
for n in CANDIDATES:
    try:
        k = parse_indicator_name(n)["kind"]
        kinds.setdefault(k, []).append(n)
    except IndicatorNameError:
        fails.append(n)

print(f"=== 可解析 kind: {len(kinds)} ===")
for k, v in sorted(kinds.items()):
    print(f"  {k:16s} 代表名: {v[0]}   （共 {len(v)} 个测试名通过）")

print()
print(f"=== 未通过: {len(fails)} ===")
for f in fails:
    print("  ", f)
