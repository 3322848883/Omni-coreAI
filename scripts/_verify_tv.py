import sys
sys.path.insert(0, ".")
import math

print("=" * 60)
print("指标验证：Python 实现 vs TV Pine Script 算法")
print("=" * 60)

# ── 1. Linreg: 验证 slope/intercept 计算 ──
print("\n--- 1. Linear Regression (calc_slope) ---")
# TV 公式: slope = (length * sumXY - sumX * sumY) / (length * sumXSqr - sumX * sumX)
# intercept = average - slope * sumX / length + slope
values = [10.0, 12.0, 14.0, 16.0, 18.0]  # 线性 +2/步
length = 5
sum_x = sum(range(1, length + 1))  # 1+2+3+4+5 = 15
sum_y = sum(values)  # 70
sum_xy = sum((i + 1) * values[i] for i in range(length))  # 1*10+2*12+3*14+4*16+5*18 = 240
sum_xx = sum((i + 1) ** 2 for i in range(length))  # 1+4+9+16+25 = 55

slope_tv = (length * sum_xy - sum_x * sum_y) / (length * sum_xx - sum_x * sum_x)
average = sum_y / length
intercept_tv = average - slope_tv * sum_x / length + slope_tv

print(f"  TV 公式 slope = ({length}*{sum_xy} - {sum_x}*{sum_y}) / ({length}*{sum_xx} - {sum_x}^2)")
print(f"           = ({length*sum_xy} - {sum_x*sum_y}) / ({length*sum_xx} - {sum_x*sum_x})")
print(f"           = {slope_tv}")
print(f"  TV 公式 intercept = {average} - {slope_tv}*{sum_x}/{length} + {slope_tv}")
print(f"                  = {intercept_tv}")

from gate_bot.strategist.tv_indicators import calc_slope
slope_py, avg_py, intercept_py = calc_slope(values, length)
print(f"  Python calc_slope: slope={slope_py}, intercept={intercept_py}")
assert abs(slope_py - slope_tv) < 1e-10, f"slope mismatch: {slope_py} vs {slope_tv}"
assert abs(intercept_py - intercept_tv) < 1e-10, f"intercept mismatch: {intercept_py} vs {intercept_tv}"
print("  ✅ MATCH")

# ── 2. RSI: 验证 TV ta.rma 平滑 ──
print("\n--- 2. RSI (TV ta.rma smoothing) ---")
# TV RSI: up = ta.rma(max(change(src), 0), len) / dn = ta.rma(-min(change(src), 0), len)
# rsi = dn == 0 ? 100 : up == 0 ? 0 : 100 - 100 / (1 + up / dn)
closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
          45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
rsi_len = 14

# TV: change = src - src[1]
changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
# TV: up = max(change, 0), dn = max(-change, 0)
ups = [max(c, 0) for c in changes]
dns = [max(-c, 0) for c in changes]

# TV: ta.rma = Wilder's RMA
def rma_tv(data, period):
    result = [None] * len(data)
    if len(data) < period:
        return result
    result[period - 1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        result[i] = (result[i-1] * (period - 1) + data[i]) / period
    return result

up_rma = rma_tv(ups, rsi_len)
dn_rma = rma_tv(dns, rsi_len)

# TV RSI formula
rsi_tv = [None] * len(closes)
for i in range(len(closes)):
    idx = i - 1  # change array is offset by 1
    if idx < rsi_len - 1 or up_rma[idx] is None or dn_rma[idx] is None:
        continue
    up = up_rma[idx]
    dn = dn_rma[idx]
    if dn == 0:
        rsi_tv[i] = 100
    elif up == 0:
        rsi_tv[i] = 0
    else:
        rsi_tv[i] = 100 - 100 / (1 + up / dn)

from gate_bot.strategist.tv_indicators import rsi_base
rsi_py = rsi_base(closes, rsi_len)

# Compare last non-None values
last_tv = [v for v in rsi_tv if v is not None][-1]
last_py = [v for v in rsi_py if v is not None][-1]
print(f"  TV RSI last:   {last_tv:.4f}")
print(f"  Python RSI last: {last_py:.4f}")
assert abs(last_tv - last_py) < 0.01, f"RSI mismatch: {last_tv} vs {last_py}"
print("  ✅ MATCH")

# ── 3. T3: 验证 GD 算法 ──
print("\n--- 3. T3 Moving Average (Tillson) ---")
# TV: _gd(src, length, alpha) => e1 = ta.ema(src, length); e1*(1+alpha) - ta.ema(e1, length)*alpha
# _t3 = _gd(_gd(_gd(src, length, alpha), length, alpha), length, alpha)
t3_values = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0]
t3_len = 3
t3_alpha = 0.7

def ema_tv(data, length):
    k = 2 / (length + 1)
    result = [None] * len(data)
    if data:
        result[0] = data[0]
        for i in range(1, len(data)):
            result[i] = data[i] * k + result[i-1] * (1 - k)
    return result

def gd_tv(src, length, alpha):
    e1 = ema_tv(src, length)
    e2 = ema_tv([v if v is not None else 0 for v in e1], length)
    return [(e1[i] * (1 + alpha) - e2[i] * alpha) if e1[i] is not None else None
            for i in range(len(src))]

g1 = gd_tv(t3_values, t3_len, t3_alpha)
g2 = gd_tv([v if v is not None else 0 for v in g1], t3_len, t3_alpha)
g3 = gd_tv([v if v is not None else 0 for v in g2], t3_len, t3_alpha)
t3_tv = g3[-1]

from gate_bot.strategist.tv_indicators import t3_moving_average
t3_py = t3_moving_average(t3_values, t3_len, t3_alpha)
t3_py_last = t3_py[-1]

print(f"  TV T3 last:    {t3_tv:.6f}")
print(f"  Python T3 last: {t3_py_last:.6f}")
assert abs(t3_tv - t3_py_last) < 0.01, f"T3 mismatch: {t3_tv} vs {t3_py_last}"
print("  ✅ MATCH")

print("\n" + "=" * 60)
print("3 个指标全部与 TV 算法一致 ✅")
print("=" * 60)