import sys
sys.path.insert(0, ".")
import math

results = []

# 1. Linear Regression
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
results.append(("Linear Regression", abs(slope_py - slope_tv) < 1e-10 and abs(intercept_py - intercept_tv) < 1e-10))

# 2. RSI
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
tv_rsi = {}
for i in range(len(changes)):
    if up_rma[i] is not None and dn_rma[i] is not None:
        u, d = up_rma[i], dn_rma[i]
        tv_rsi[i+1] = 100 if d == 0 else (0 if u == 0 else 100 - 100 / (1 + u/d))
from omnialpha.strategist.tv_indicators import rsi_base
py_rsi = {i: v for i, v in enumerate(rsi_base(closes, 14)) if v is not None}
rsi_match = all(abs(tv_rsi[k] - py_rsi.get(k+1, 0)) < 0.01 for k in tv_rsi)
results.append(("RSI (ta.rma)", rsi_match))

# 3. T3
t3_vals = [10.0 + i for i in range(11)]
def ema(data, length):
    k = 2 / (length + 1)
    out = [None] * len(data)
    if data: out[0] = data[0]
    for i in range(1, len(data)): out[i] = data[i] * k + out[i-1] * (1 - k)
    return out
def gd(src, length, alpha):
    e1 = ema(src, length)
    e2 = ema([v if v is not None else 0 for v in e1], length)
    return [(e1[i] * (1 + alpha) - e2[i] * alpha) if e1[i] is not None else None for i in range(len(src))]
g1 = gd(t3_vals, 3, 0.7)
g2 = gd([v if v is not None else 0 for v in g1], 3, 0.7)
g3 = gd([v if v is not None else 0 for v in g2], 3, 0.7)
t3_tv = g3[-1]
from omnialpha.strategist.tv_indicators import t3_moving_average
t3_py = t3_moving_average(t3_vals, 3, 0.7)[-1]
results.append(("T3 Moving Average", abs(t3_tv - t3_py) < 0.01))

# 4. KAMA
kama_vals = [10.0 + i * 0.5 for i in range(20)]
def kama_tv(values, period):
    n = len(values)
    out = [None] * n
    if n < period + 1: return out
    prev = values[period - 1]
    out[period - 1] = prev
    for i in range(period, n):
        chg = abs(values[i] - values[i - period])
        vol = sum(abs(values[j] - values[j-1]) for j in range(i-period+1, i+1))
        er = chg / vol if vol > 0 else 0
        sc = (er * (2/3 - 2/31) + 2/31) ** 2
        prev = prev + sc * (values[i] - prev)
        out[i] = prev
    return out
kama_tv_val = kama_tv(kama_vals, 10)[-1]
from omnialpha.strategist.tv_indicators import kama
kama_py_val = kama(kama_vals, 10)[-1]
results.append(("KAMA", abs(kama_tv_val - kama_py_val) < 0.01))

# 5. Heikin Ashi
o = [10.0, 11.0, 12.0, 13.0]
h = [11.0, 12.0, 13.0, 14.0]
l = [9.0, 10.0, 11.0, 12.0]
c = [10.5, 11.5, 12.5, 13.5]
ha_close = [(o[i]+h[i]+l[i]+c[i])/4 for i in range(4)]
ha_open = [None]*4
ha_open[0] = (o[0]+c[0])/2
for i in range(1,4): ha_open[i] = (ha_open[i-1]+ha_close[i-1])/2
from omnialpha.strategist.tv_indicators import heikin_ashi
ha_py = heikin_ashi(o, h, l, c)
ha_match = all(abs(ha_open[i] - ha_py["open"][i]) < 0.01 for i in range(4))
results.append(("Heikin Ashi", ha_match))

# 6. Volatility Bands
vb_h = [11.0, 12.0, 13.0, 14.0, 15.0] * 4
vb_l = [9.0, 10.0, 11.0, 12.0, 13.0] * 4
vb_c = [10.0, 11.0, 12.0, 13.0, 14.0] * 4
from omnialpha.strategist.tv_indicators import volatility_bands
vb = volatility_bands(vb_h, vb_l, vb_c, length=5)
tv_upper = vb["basis"][-1] + 2.0 * vb["atr"][-1]
vb_match = abs(vb["upper_inner"][-1] - tv_upper) < 0.01
results.append(("Volatility Bands", vb_match))

print("=" * 50)
print("TV Pine Script 算法一致性验证")
print("=" * 50)
all_ok = True
for name, ok in results:
    status = "✅" if ok else "❌"
    print(f"  {status} {name}")
    all_ok = all_ok and ok
print("=" * 50)
print(f"结果: {'全部一致 ✅' if all_ok else '存在不一致 ❌'}")