import sys, json, urllib.request
sys.path.insert(0, ".")
from pathlib import Path
from gate_bot.strategist.vision import generate_candlestick_chart
from gate_bot.strategist.indicators import ema

def fetch(interval, limit):
    url = f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=BTC_USDT&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())

# 拉 5m 100 根（20 warmup + 80 display）
all_k = fetch("5m", 100)
warmup, display = all_k[:20], all_k[20:]
closes = [float(k["c"]) for k in all_k]
ema20_full = ema(closes, 20)
ema20_display = ema20_full[20:]

png = generate_candlestick_chart(
    display, symbol="BTC_USDT", timeframe="5m",
    output_path="data/btc_5m_compare.png",
    indicators={"ema20": ema20_display},
    bar_count=True, warmup_klines=warmup,
)
print(f"Generated: {len(png)} bytes")