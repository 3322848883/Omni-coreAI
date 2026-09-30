"""TV indicators vs TradingView — independent-reference consistency tests.

不与被测实现共享算法代码：
  - linreg：独立最小二乘（x=0..n-1 时序）作金标准
  - RSI：Wilder《New Concepts》经典数据集公开值 + 独立 RMA
  - HA/T3/KAMA：按 Pine 文档公式独立重算
  - 另与 gate_bot.strategist.indicators 交叉对账
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gate_bot.strategist.tv_indicators import (  # noqa: E402
    calc_dev,
    calc_slope,
    heikin_ashi,
    kama,
    linreg_channel,
    lr_ha_candles,
    ob_os_signals,
    rsi_base,
    rsi_bollinger,
    rsi_histogram,
    rsi_macd,
    rsi_ma,
    rsi_smoothed,
    swing_structure,
    t3_moving_average,
    trendlines,
    volatility_bands,
)
from gate_bot.strategist.tv_indicators.lr_ha_candles import _linreg_val  # noqa: E402
from gate_bot.strategist.indicators import linreg as ind_linreg  # noqa: E402
from gate_bot.strategist.indicators import rsi as ind_rsi  # noqa: E402


# ── 独立金标准 ─────────────────────────────────────

def ref_ols(values: list[float]) -> tuple[float, float]:
    """独立最小二乘：x=0..n-1（时序）。返回 (slope, intercept@x=0)。"""
    n = len(values)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    den = sum((x - mx) ** 2 for x in xs)
    slope = num / den
    intercept = my - slope * mx
    return slope, intercept


def ref_linreg_value(values: list[float], length: int, offset: int = 0) -> float:
    """ta.linreg(src, length, offset)：窗口末点向前 offset 根的拟合值。"""
    seg = values[-length:]
    slope, intercept = ref_ols(seg)
    # x=0..length-1，末点 x=length-1，offset 向未来投影
    return intercept + slope * (length - 1 + offset)


def ref_rma(data: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(data)
    if len(data) < period:
        return out
    prev = sum(data[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(data)):
        prev = (prev * (period - 1) + data[i]) / period
        out[i] = prev
    return out


def ref_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """TV ta.rsi：change → max/min → 双 RMA → 100-100/(1+up/dn)。"""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    up = ref_rma(gains, period)
    dn = ref_rma(losses, period)
    for i in range(n):
        u = up[i - 1] if i >= 1 else None
        d = dn[i - 1] if i >= 1 else None
        if u is None or d is None:
            continue
        if d == 0:
            out[i] = 100.0
        elif u == 0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + u / d)
    return out


def ref_heikin_ashi(o, h, l, c):
    n = len(c)
    ho, hh, hl, hc = [None] * n, [None] * n, [None] * n, [None] * n
    for i in range(n):
        hc[i] = (o[i] + h[i] + l[i] + c[i]) / 4.0
        if i == 0:
            ho[i] = (o[i] + c[i]) / 2.0
        else:
            ho[i] = (ho[i - 1] + hc[i - 1]) / 2.0
        hh[i] = max(h[i], ho[i], hc[i])
        hl[i] = min(l[i], ho[i], hc[i])
    return {"open": ho, "high": hh, "low": hl, "close": hc}


# Wilder《New Concepts in Technical Trading Systems》经典 RSI 样本
# 前 15 个收盘价，14 周期 RSI 首值公开为 70.53（StockCharts/Wilder）
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
    45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
]
WILDER_RSI14_FIRST = 70.53  # 公开值，允许 0.02 舍入差


class TestLinregVsOLS(unittest.TestCase):
    """linreg / calc_slope 与独立最小二乘对照。"""

    def test_calc_slope_matches_ols_slope(self):
        vals = [10.0, 12.1, 13.8, 16.2, 18.0, 20.3, 21.9, 24.1]
        length = 8
        slope, avg, intercept = calc_slope(vals, length)
        ols_slope, ols_b0 = ref_ols(vals[-length:])
        self.assertAlmostEqual(slope, ols_slope, places=10)
        self.assertAlmostEqual(avg, sum(vals[-length:]) / length, places=10)

    def test_calc_slope_intercept_is_window_start(self):
        # intercept = 窗口最老一根（x=0）的拟合值；与 x=0..n-1 的 OLS 截距一致
        vals = [10.0, 12.1, 13.8, 16.2, 18.0, 20.3, 21.9, 24.1]
        length = 8
        slope, avg, intercept = calc_slope(vals, length)
        ols_slope, ols_b0 = ref_ols(vals[-length:])
        self.assertAlmostEqual(intercept, ols_b0, places=10)
        self.assertAlmostEqual(slope, ols_slope, places=10)

    def test_perfect_line_exact(self):
        vals = [2.0 * i + 5.0 for i in range(20)]
        slope, avg, intercept = calc_slope(vals, 20)
        self.assertAlmostEqual(slope, 2.0, places=10)
        ols_slope, ols_b0 = ref_ols(vals)
        self.assertAlmostEqual(intercept, ols_b0, places=10)
        # 最新一根拟合值 = intercept + slope*(n-1)
        self.assertAlmostEqual(intercept + slope * 19, vals[-1], places=10)

    def test_linreg_val_matches_ols_projection(self):
        vals = [100.0 + math.sin(i / 3) * 5 + i * 0.3 for i in range(50)]
        for period in (5, 9, 14):
            for offset in (-1, 0, 1, 2):
                got = _linreg_val(vals, period, offset)
                for i in range(period - 1, len(vals)):
                    exp = ref_linreg_value(vals[: i + 1], period, offset)
                    self.assertAlmostEqual(
                        got[i], exp, places=8,
                        msg=f"period={period} offset={offset} bar={i}",
                    )

    def test_linreg_val_matches_indicators_linreg(self):
        vals = [50.0 + (i % 7) - 3 + i * 0.1 for i in range(60)]
        for period in (5, 9, 14, 20):
            a = _linreg_val(vals, period, 0)
            b = ind_linreg(vals, period)
            for i in range(len(vals)):
                if a[i] is None and b[i] is None:
                    continue
                self.assertIsNotNone(a[i], f"tv None at {i} period={period}")
                self.assertIsNotNone(b[i], f"ind None at {i} period={period}")
                self.assertAlmostEqual(a[i], b[i], places=8,
                                       msg=f"period={period} bar={i}")

    def test_linreg_channel_spread_is_k_sigma(self):
        vals = [100.0 + math.sin(i / 4) * 3 for i in range(80)]
        ch = linreg_channel(vals, [v + 0.2 for v in vals], vals, length=40,
                            upper_mult=2.0, lower_mult=2.0)
        self.assertIsNotNone(ch["base"]["start"])
        up = ch["upper"]["start"] - ch["base"]["start"]
        lo = ch["base"]["start"] - ch["lower"]["start"]
        self.assertAlmostEqual(up, 2.0 * ch["std_dev"], places=8)
        self.assertAlmostEqual(lo, 2.0 * ch["std_dev"], places=8)

    def test_linreg_channel_slope_matches_ols(self):
        vals = [10.0 + i * 0.5 for i in range(30)]
        ch = linreg_channel(vals, [v + 1 for v in vals], vals, length=30)
        ols_slope, _ = ref_ols(vals[-30:])
        self.assertAlmostEqual(ch["slope"], ols_slope, places=10)

    def test_linreg_channel_start_end_chronological(self):
        vals = [10.0 + i * 0.5 for i in range(30)]
        ch = linreg_channel(vals, [v + 1 for v in vals], [v - 1 for v in vals],
                            length=30)
        # start = 窗口最老一根拟合值，end = 最新一根
        self.assertAlmostEqual(ch["base"]["start"], 10.0, places=8)
        self.assertAlmostEqual(ch["base"]["end"], vals[-1], places=8)
        self.assertLess(ch["base"]["start"], ch["base"]["end"])  # 上升趋势


class TestRSIWilder(unittest.TestCase):
    """RSI 与 Wilder 公开值 / 独立 RMA 对照。"""

    def test_wilder_classic_first_value(self):
        # 经典 15 个收盘价 → 首个 14 周期 RSI
        out = rsi_base(WILDER_CLOSES, 14)
        vals = [v for v in out if v is not None]
        self.assertTrue(vals, "rsi_base produced no values")
        # StockCharts/Wilder 公开值 70.53；本地精确式 70.46（中间步骤舍入差）
        self.assertAlmostEqual(vals[0], WILDER_RSI14_FIRST, delta=0.1,
                               msg=f"first RSI={vals[0]}")
        # 与独立实现完全一致
        ref = ref_rsi(WILDER_CLOSES, 14)
        self.assertAlmostEqual(vals[0], ref[14], places=8)

    def test_rsi_base_matches_independent_rsi(self):
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
                  46.03, 46.41, 46.22, 45.64, 46.25, 46.60, 46.70, 46.90]
        a = rsi_base(closes, 14)
        b = ref_rsi(closes, 14)
        # 对齐比较：找出双方首个非 None
        ai = next(i for i, v in enumerate(a) if v is not None)
        bi = next(i for i, v in enumerate(b) if v is not None)
        # 关键：TV 首 RSI 落在 close index = period（14），不是 period+1
        self.assertEqual(bi, 14, f"ref first idx={bi}")
        self.assertEqual(ai, bi, f"rsi_base first idx={ai} != ref {bi} (off-by-one?)")
        for i in range(bi, len(closes)):
            self.assertIsNotNone(a[i], f"rsi_base[{i}] None")
            self.assertAlmostEqual(a[i], b[i], places=8, msg=f"bar={i}")

    def test_rsi_base_matches_indicators_rsi(self):
        closes = [100.0 + math.sin(i / 5) * 8 + (i % 3) for i in range(80)]
        a = rsi_base(closes, 14)
        b = ind_rsi(closes, 14)
        for i in range(len(closes)):
            if a[i] is None and b[i] is None:
                continue
            self.assertIsNotNone(a[i], f"tv None at {i}")
            self.assertIsNotNone(b[i], f"ind None at {i}")
            self.assertAlmostEqual(a[i], b[i], places=8, msg=f"bar={i}")

    def test_rsi_all_up_100_all_down_0(self):
        up = [float(i) for i in range(1, 30)]
        self.assertAlmostEqual(rsi_base(up, 14)[-1], 100.0)
        down = [float(40 - i) for i in range(30)]
        self.assertAlmostEqual(rsi_base(down, 14)[-1], 0.0)

    def test_rsi_bands_brackets_ma(self):
        closes = [100.0 + math.sin(i / 4) * 10 for i in range(60)]
        rsi_v = rsi_base(closes, 14)
        bb = rsi_bollinger(rsi_v, length=21, mult=2.0)
        checked = 0
        for i in range(len(rsi_v)):
            if bb["upper"][i] is None:
                continue
            self.assertGreaterEqual(bb["upper"][i], bb["ma"][i])
            self.assertLessEqual(bb["lower"][i], bb["ma"][i])
            checked += 1
        self.assertGreater(checked, 5)

    def test_rsi_candles_and_hist(self):
        closes = [100.0 + (i % 5) * 2 for i in range(40)]
        rsi_v = rsi_base(closes, 14)
        ma = rsi_ma(rsi_v, 21, "SMA")
        hist = rsi_histogram(rsi_v, ma)
        for i in range(len(rsi_v)):
            if rsi_v[i] is None or ma[i] is None:
                self.assertIsNone(hist[i])
            else:
                self.assertAlmostEqual(hist[i], rsi_v[i] - ma[i], places=10)

    def test_ob_os_signals(self):
        # 上穿 70：65→72；下穿 30：32→28
        rsi_v = [None, 65.0, 72.0, 75.0, 68.0, 32.0, 28.0]
        sig = ob_os_signals(rsi_v, 70, 30)
        self.assertFalse(sig["overbought"][1])
        self.assertTrue(sig["overbought"][2])   # 65→72 上穿
        self.assertFalse(sig["overbought"][3])
        self.assertFalse(sig["oversold"][5])     # 68→32 未跌破 30
        self.assertTrue(sig["oversold"][6])      # 32→28 下穿

    def test_rsi_smoothed_monotone_input(self):
        rsi_v = [float(i) for i in range(30)]
        sm = rsi_smoothed(rsi_v, 3)
        self.assertIsNotNone(sm[-1])
        self.assertGreater(sm[-1], sm[0])


class TestHeikinAshiAndLR(unittest.TestCase):
    def test_ha_matches_reference(self):
        o = [10.0, 11.0, 12.0, 13.0, 12.5, 13.0]
        h = [11.5, 12.0, 13.5, 14.0, 13.0, 14.2]
        l = [9.5, 10.5, 11.5, 12.0, 12.0, 12.8]
        c = [11.0, 11.5, 13.0, 12.5, 12.8, 14.0]
        got = heikin_ashi(o, h, l, c)
        exp = ref_heikin_ashi(o, h, l, c)
        for key in ("open", "high", "low", "close"):
            for i in range(len(o)):
                self.assertAlmostEqual(got[key][i], exp[key][i], places=10,
                                       msg=f"{key}[{i}]")

    def test_ha_open_is_midpoint_chain(self):
        o = [10.0, 11.0, 12.0]
        h = [11.0, 12.0, 13.0]
        l = [9.0, 10.0, 11.0]
        c = [10.5, 11.5, 12.5]
        ha = heikin_ashi(o, h, l, c)
        self.assertAlmostEqual(ha["open"][1], (ha["open"][0] + ha["close"][0]) / 2)

    def test_lr_ha_candles_align_with_period(self):
        n = 40
        o = [100.0 + i * 0.2 for i in range(n)]
        h = [v + 1.0 for v in o]
        l = [v - 1.0 for v in o]
        c = [v + 0.3 for v in o]
        for length in (5, 9):
            got = lr_ha_candles(o, h, l, c, length=length)
            ha = ref_heikin_ashi(o, h, l, c)
            for key in ("open", "high", "low", "close"):
                self.assertIsNone(got[key][length - 2], f"{key} early not None")
                self.assertIsNotNone(got[key][length - 1], f"{key} missing")
                exp = ref_linreg_value(
                    [float(x) for x in ha[key][:length]], length, 0)
                self.assertAlmostEqual(got[key][length - 1], exp, places=8)

    def test_t3_matches_reference_gd(self):
        src = [10.0 + math.sin(i / 2) * 2 for i in range(40)]

        def ema(data, length):
            k = 2 / (length + 1)
            out = [None] * len(data)
            out[0] = data[0]
            for i in range(1, len(data)):
                out[i] = data[i] * k + out[i - 1] * (1 - k)
            return out

        def gd(data, length, alpha):
            e1 = ema(data, length)
            e2 = ema([v or 0 for v in e1], length)
            return [(e1[i] * (1 + alpha) - e2[i] * alpha) for i in range(len(data))]

        length, alpha = 5, 0.7
        exp = gd(gd(gd(src, length, alpha), length, alpha), length, alpha)
        got = t3_moving_average(src, length, alpha)
        self.assertAlmostEqual(got[-1], exp[-1], places=8)
        self.assertAlmostEqual(got[-5], exp[-5], places=8)

    def test_volatility_bands_formula(self):
        n = 30
        h = [101.0 + i * 0.1 for i in range(n)]
        l = [99.0 + i * 0.1 for i in range(n)]
        c = [100.0 + i * 0.1 for i in range(n)]
        vb = volatility_bands(h, l, c, length=10, upper_inner=2.0,
                              lower_inner=2.0, upper_outer=3.0, lower_outer=3.0,
                              basis_type="EMA")
        for i in range(n):
            if vb["basis"][i] is None:
                continue
            self.assertAlmostEqual(vb["upper_inner"][i],
                                   vb["basis"][i] + 2.0 * vb["atr"][i], places=8)
            self.assertAlmostEqual(vb["lower_outer"][i],
                                   vb["basis"][i] - 3.0 * vb["atr"][i], places=8)

    def test_kama_reference(self):
        vals = [10.0 + math.sin(i / 3) * 2 + i * 0.05 for i in range(30)]
        period = 10
        # 独立重算
        n = len(vals)
        exp = [None] * n
        fastest, slowest = 2 / 3, 2 / 31
        prev = vals[period - 1]
        exp[period - 1] = prev
        for i in range(period, n):
            chg = abs(vals[i] - vals[i - period])
            vol = sum(abs(vals[j] - vals[j - 1]) for j in range(i - period + 1, i + 1))
            er = chg / vol if vol > 0 else 0.0
            sc = (er * (fastest - slowest) + slowest) ** 2
            prev = prev + sc * (vals[i] - prev)
            exp[i] = prev
        got = kama(vals, period)
        self.assertAlmostEqual(got[-1], exp[-1], places=10)
        self.assertAlmostEqual(got[period], exp[period], places=10)


class TestTrendlines(unittest.TestCase):
    def test_pivot_trendline_on_synthetic(self):
        # 明确的两段摆动高点 / 低点
        # pivot highs at idx 8, 18, 28 ; pivot lows at idx 3, 13, 23
        highs = [9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0,
                 12.5, 12.0, 11.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0,
                 13.5, 13.0, 12.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0,
                 14.5, 14.0]
        lows = [v - 1.2 for v in highs]
        # 手工加深摆动低点
        for i in (3, 13, 23):
            lows[i] = highs[i] - 2.5
        closes = [v - 0.5 for v in highs]
        opens = [v - 0.8 for v in highs]
        tl = trendlines(highs, lows, closes, opens, lookback=5)
        self.assertIn("primary", tl)
        self.assertIn("secondary", tl)
        prim = tl["primary"]
        self.assertIsNotNone(prim["upper"], "no pivot-high trendline")
        self.assertIsNotNone(prim["lower"], "no pivot-low trendline")
        # 上轨斜率应为正（高点抬高）
        self.assertGreater(prim["upper"]["slope"], 0)

    def test_short_series_returns_nones(self):
        tl = trendlines([1.0, 2.0], [0.5, 1.5], [1.0, 2.0], [1.0, 2.0], lookback=25)
        self.assertIsNone(tl["primary"]["upper"])
        self.assertIsNone(tl["primary"]["lower"])


class TestCalcDev(unittest.TestCase):
    def test_std_dev_nonnegative_and_pearson_bounded(self):
        closes = [100.0 + math.sin(i / 3) * 2 for i in range(50)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        slope, avg, intercept = calc_slope(closes, 50)
        std, pearson, up_dev, dn_dev = calc_dev(
            highs, lows, closes, 50, slope, avg, intercept)
        self.assertGreaterEqual(std, 0.0)
        self.assertLessEqual(abs(pearson), 1.0 + 1e-9)
        self.assertGreaterEqual(up_dev, 0.0)
        self.assertGreaterEqual(dn_dev, 0.0)

    def test_linreg_channel_three_layers(self):
        """TV 截图通道为 3 层（1σ/2σ/3σ 实线/虚线/点线）+ 中轴。"""
        vals = [100.0 + i * 0.2 for i in range(40)]
        highs = [v + 1.0 for v in vals]
        lows = [v - 1.0 for v in vals]
        ch = linreg_channel(vals, highs, lows, length=40)
        self.assertEqual(len(ch["layers"]), 3)
        mults = [layer["mult"] for layer in ch["layers"]]
        self.assertEqual(mults, [1.0, 2.0, 3.0])
        # 内层 < 中层 < 外层（上轨）
        u0 = ch["layers"][0]["upper"]["end"]
        u1 = ch["layers"][1]["upper"]["end"]
        u2 = ch["layers"][2]["upper"]["end"]
        self.assertLess(u0, u1)
        self.assertLess(u1, u2)
        # 每层 = base ± k*std_dev
        base_end = ch["base"]["end"]
        self.assertAlmostEqual(u0 - base_end, 1.0 * ch["std_dev"], places=8)
        self.assertAlmostEqual(u2 - base_end, 3.0 * ch["std_dev"], places=8)
        # 下轨对称
        l0 = ch["layers"][0]["lower"]["end"]
        self.assertAlmostEqual(base_end - l0, 1.0 * ch["std_dev"], places=8)


class TestSwingStructure(unittest.TestCase):
    """TV 图上 HH/HL/LH/LL 标签。"""

    def test_hh_ll_sequence(self):
        # 构造：低点 10 → 高点 20 → 低点 12(HL) → 高点 25(HH) → 低点 8(LL)
        vals = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                19, 18, 17, 16, 15, 14, 13, 12, 13, 14,
                15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
                24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8,
                9, 10]
        labels = swing_structure(vals, left=3, right=3)
        tagged = [(i, v, labels[i]) for i, v in enumerate(vals) if labels[i]]
        # 应有枢轴标签
        self.assertTrue(any(lab in ("HH", "LH", "H") for _, _, lab in tagged))
        self.assertTrue(any(lab in ("HL", "LL", "L") for _, _, lab in tagged))

    def test_monotone_has_no_pivot(self):
        vals = [float(i) for i in range(20)]
        labels = swing_structure(vals, left=2, right=2)
        self.assertTrue(all(x is None for x in labels))

    def test_labels_match_rsi_chart_pattern(self):
        # 模拟截图 1 的 RSI：先冲高（HH）再回落（LH）再探底（LL）
        rsi = [30, 35, 40, 45, 50, 55, 60, 65, 70, 68,
               66, 64, 62, 60, 58, 55, 52, 50, 48, 50,
               52, 55, 58, 60, 62, 64, 66, 65, 63, 61,
               58, 55, 50, 45, 40, 35, 30, 28, 26, 25,
               27, 29, 31, 33, 35, 37, 39, 41, 43, 45]
        labels = swing_structure(rsi, left=3, right=3)
        tagged = [(i, v, labels[i]) for i, v in enumerate(rsi) if labels[i]]
        self.assertGreaterEqual(len(tagged), 2)
        kinds = {lab for _, _, lab in tagged}
        self.assertTrue(kinds & {"HH", "LH", "H"})
        self.assertTrue(kinds & {"HL", "LL", "L"})


class TestRsiMacd(unittest.TestCase):
    """TV 设置里的 12/26/9 MACD on RSI。"""

    def test_macd_identity(self):
        closes = [100.0 + math.sin(i / 5) * 8 for i in range(80)]
        rsi_v = rsi_base(closes, 14)
        m = rsi_macd(rsi_v, 12, 26, 9)
        for key in ("macd", "signal", "hist"):
            self.assertEqual(len(m[key]), len(rsi_v))
        checked = 0
        for i in range(len(rsi_v)):
            if m["hist"][i] is None:
                continue
            self.assertAlmostEqual(
                m["hist"][i], m["macd"][i] - m["signal"][i], places=8)
            checked += 1
        self.assertGreater(checked, 5)

    def test_macd_flat_series_near_zero(self):
        rsi_v = [50.0] * 40
        m = rsi_macd(rsi_v, 12, 26, 9)
        self.assertAlmostEqual(m["macd"][-1], 0.0, places=8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
