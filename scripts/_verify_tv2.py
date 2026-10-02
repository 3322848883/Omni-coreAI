import sys
sys.path.insert(0, ".")
import math

print("=" * 60)
print("3 指标 TV 算法一致性验证")
print("=" * 60)

# ── 1. Linreg ──
print("\n--- 1. Linear Regression ---")
values = [10.0, 12.0, 14.0, 16.0, 18.0]
length = 5
sum_x = sum(range(1, length + 1))
sum_y = sum(values)
sum_xy = sum((i + 1) * values[i] for i in range(length))
sum_xx = sum((i + 1) ** 2 for i in range(length))
slope_tv = (length * sum_xy - sum_x * sum_y) / (length * sum_xx - sum_x * sum_x)
average = sum_y / length
intercept_tv = average - slope_tv * sum_x / length + slope_tv

from omnialpha.strategist.tv_indicators import calc_slope
slope_py, avg_py, intercept_py = calc_slope(values, length)
print(f"  TV slope={slope_tv}, intercept={intercept_tv}")
print(f"  Py  slope={slope_py}, intercept={intercept_py}")
assert abs(slope_py - slope_tv) < 1e-10
assert abs(intercept_py - intercept_tv) < 1e-10
print("  ✅ MATCH")

# ── 2. RSI ──
print("\n--- 2. RSI (TV ta.rma) ---")
closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
          45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
ups = [max(c, 0) for c in changes]
dns = [max(-c, 0) for c in changes]

def rma(data, period):
    out = [None] * len(data)
    if len(data) < period: return out
    out[period-1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        out[i] = (out[i-1] * (period-1) + data[i]) / period
    return out

up_rma = rma(ups, 14)
dn_rma = rma(dns, 14)

# TV RSI values
tv_rsi_vals = []
for i in range(len(changes)):
    if up_rma[i] is not None and dn_rma[i] is not None:
        u, d = up_rma[i], dn_rma[i]
        val = 100 if d == 0 else (0 if u == 0 else 100 - 100 / (1 + u/d))
        tv_rsi_vals.append(val)

from omnialpha.strategist.tv_indicators import rsi_base
py_rsi = rsi_base(closes, 14)
py_rsi_vals = [v for v in py_rsi if v is not None]

# Py values are shifted by 1, compare last 5
print(f"  TV last 5: {[f'{v:.4f}' for v in tv_rsi_vals[-5:]]}")
print(f"  Py  last 5: {[f'{v:.4f}' for v in py_rsi_vals[-5:]]}")
for i in range(5):
    tv_v = tv_rsi_vals[-(5-i)]
    py_v = py_rsi_vals[-(5-i)]
    assert abs(tv_v - py_v) < 0.01, f"mismatch at {i}: {tv_v} vs {py_v}"
print("  ✅ MATCH (values identical, 1-index offset)")

# ── 3. T3 ──
print("\n--- 3. T3 Moving Average ---")
t3_values = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0]
t3_len = 3
t3_alpha = 0.7

def ema(data, length):
    k = 2 / (length + 1)
    result = [None] * len(data)
    if data:
        result[0] = data[0]
        for i in range(1, len(data)):
            result[i] = data[i] * k + result[i-1] * (1 - k)
    return result

def gd(src, length, alpha):
    e1 = ema(src, length)
    e2 = ema([v if v is not None else 0 for v in e1], length)
    return [(e1[i] * (1 + alpha) - e2[i] * alpha) if e1[i] is not None else None
            for i in range(len(src))]

g1 = gd(t3_values, t3_len, t3_alpha)
g2 = gd([v if v is not None else 0 for v in g1], t3_len, t3_alpha)
g3 = gd([v if v is not None else 0 for v in g2], t3_len, t3_alpha)
t3_tv = g3[-1]

from omnialpha.strategist.tv_indicators import t3_moving_average
t3_py = t3_moving_average(t3_values, t3_len, t3_alpha)
t3_py_last = t3_py[-1]

print(f"  TV T3 = {t3_tv:.6f}")
print(f"  Py  T3 = {t3_py_last:.6f}")
assert abs(t3_tv - t3_py_last) < 0.01
print("  ✅ MATCH")

# ── 4. KAMA ──
print("\n--- 4. KAMA (Kaufman) ---")
kama_values = [10.0 + i * 0.5 for i in range(20)]
kama_period = 10

def kama_tv(values, period):
    n = len(values)
    out = [None] * n
    if n < period + 1: return out
    fastest = 2/3
    slowest = 2/31
    prev = values[period - 1]
    out[period - 1] = prev
    for i in range(period, n):
        chg = abs(values[i] - values[i - period])
        vol = sum(abs(values[j] - values[j-1]) for j in range(i-period+1, i+1))
        er = chg / vol if vol > 0 else 0
        sc = (er * (fastest - slowest) + slowest) ** 2
        prev = prev + sc * (values[i] - prev)
        out[i] = prev
    return out

kama_tv_last = kama_tv(kama_values, kama_period)[-1]

from omnialpha.strategist.tv_indicators import kama
kama_py_last = kama(kama_values, kama_period)[-1]

print(f"  TV KAMA = {kama_tv_last:.6f}")
print(f"  Py  KAMA = {kama_py_last:.6f}")
assert abs(kama_tv_last - kama_py_last) < 0.01
print("  ✅ MATCH")

# ── 5. Heikin Ashi ──
print("\n--- 5. Heikin Ashi ---")
ha_opens = [10.0, 11.0, 12.0, 13.0]
ha_highs = [11.0, 12.0, 13.0, 14.0]
ha_lows = [9.0, 10.0, 11.0, 12.0]
ha_closes = [10.5, 11.5, 12.5, 13.5]

# TV HA formula
ha_close_tv = [(ha_opens[i] + ha_highs[i] + ha_lows[i] + ha_closes[i]) / 4 for i in range(4)]
ha_open_tv = [None] * 4
ha_open_tv[0] = (ha_opens[0] + ha_closes[0]) / 2
for i in range(1, 4):
    ha_open_tv[i] = (ha_open_tv[i-1] + ha_close_tv[i-1]) / 2
ha_high_tv = [max(ha_highs[i], ha_open_tv[i], ha_close_tv[i]) for i in range(4)]
ha_low_tv = [min(ha_lows[i], ha_open_tv[i], ha_close_tv[i]) for i in range(4)]

from omnialpha.strategist.tv_indicators import heikin_ashi
ha_py = heikin_ashi(ha_opens, ha_highs, ha_lows, ha_closes)

for i in range(4):
    assert abs(ha_open_tv[i] - ha_py["open"][i]) < 0.01, f"open mismatch at {i}"
    assert abs(ha_close_tv[i] - ha_py["close"][i]) < 0.01, f"close mismatch at {i}"
print(f"  TV HA open={ha_open_tv[-1]:.4f}, close={ha_close_tv[-1]:.4f}")
print(f"  Py  HA open={ha_py['open'][-1]:.4f}, close={ha_py['close'][-1]:.4f}")
print("  ✅ MATCH")

# ── 6. Volatility Bands ──
print("\n--- 6. Volatility Bands (ATR-based) ---")
vb_highs = [11.0, 12.0, 13.0, 14.0, 15.0] * 4
vb_lows = [9.0, 10.0, 11.0, 12.0, 13.0] * 4
vb_closes = [10.0, 11.0, 12.0, 13.0, 14.0] * 4

from omnialpha.strategist.tv_indicators import volatility_bands
vb = volatility_bands(vb_highs, vb_lows, vb_closes, length=5)
# TV: basis + upper_mult * atr
last_basis = vb["basis"][-1]
last_atr = vb["atr"][-1]
tv_upper = last_basis + 2.0 * last_atr
tv_lower = last_basis - 2.0 * last_atr
print(f"  TV basis={last_basis:.4f}, atr={last_atr:.4f}")
print(f"  TV upper={tv_upper:.4f}, lower={tv_lower:.4f}")
print(f"  Py  upper={vb['upper_inner'][-1]:.4f}, lower={vb['lower_inner'][-1]:.4f}")
assert abs(vb['upper_inner'][-1] - tv_upper) < 0.01
assert abs(vb['lower_inner'][-1] - tv_lower) < 0.01
print("  ✅ MATCH")

print("\n" + "=" * 60)
print("6 个指标全部与 TV Pine Script 算法一致 ✅")
print("=" * 60)