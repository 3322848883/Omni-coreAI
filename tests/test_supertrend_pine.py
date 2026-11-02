"""SuperTrend 必须与 Pine v6 `ta.supertrend` 逐 bar 一致。

为什么单独一个文件（2026-11-02）：
  原实现有三处偏离 Pine，且都不报错、只是「值不对」：
    1. 方向与线自相矛盾 —— `st = lower if direction==1 else upper`，而 direction==1 在
       Pine 里是**下跌**、线应取上轨（压力）；原实现给它配了下轨（支撑）。
       第一根 bar 必然 d=1 + st=下轨，图上画出来就是「下跌趋势支撑线在价格下方」。
    2. 翻转判定用 `prev_close` 比本根 band，Pine 用**本根 close** 比 band。
    3. 翻转判定基准不是「上一根线在哪条 band」，而是各自独立比较上下轨，
       在窄幅震荡里会多翻一次（Pine 里趋势状态是粘性的）。

  这个文件用**独立翻译自 Pine 源码的参考实现**做逐 bar 对照，不复用被测代码的
  任何中间函数（连 ATR 都自己写一遍），避免同一个 bug 在两份实现里同时成立。
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omnialpha.strategist.indicators import (  # noqa: E402
    attach_indicators,
    latest_indicators,
    parse_indicator_name,
    supertrend,
)


# --------------------------------------------------------------------------
# Pine v6 参考实现（逐行翻译 TradingView 内置 ta.supertrend / Supertrend 指标）
#   upper = hl2 + factor*atr        // 压力轨
#   lower = hl2 - factor*atr        // 支撑轨
#   upper := upper < prevUpper or close[1] > prevUpper ? upper : prevUpper
#   lower := lower > prevLower or close[1] < prevLower ? lower : prevLower
#   direction: 上一根线 == prevUpper ? (close > upper ? -1 : 1)
#                                  : (close < lower ? 1 : -1)
#   supertrend := direction == -1 ? lower : upper
#   direction 语义：-1 = uptrend，1 = downtrend
# --------------------------------------------------------------------------
def _tr(h, l, c):
    out = []
    for i in range(len(h)):
        if i == 0:
            out.append(h[i] - l[i])
        else:
            pc = c[i - 1]
            out.append(max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc)))
    return out


def _rma(vals, period):
    out = [None] * len(vals)
    if len(vals) < period or period <= 0:
        return out
    prev = sum(vals[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(vals)):
        prev = (prev * (period - 1) + vals[i]) / period
        out[i] = prev
    return out


def ref_supertrend(h, l, c, period=10, factor=3.0):
    atr = _rma(_tr(h, l, c), period)
    n = len(c)
    st = [None] * n
    direction = [None] * n
    upper = [None] * n
    lower = [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        hl2 = (h[i] + l[i]) / 2.0
        up = hl2 - factor * atr[i]
        dn = hl2 + factor * atr[i]
        if i > 0 and upper[i - 1] is not None:
            pu, pl, pc = upper[i - 1], lower[i - 1], c[i - 1]
            if not (dn < pu or pc > pu):
                dn = pu
            if not (up > pl or pc < pl):
                up = pl
        upper[i], lower[i] = dn, up          # 注意：upper 列存 dn（上轨）
        if i == 0 or st[i - 1] is None:
            d = 1
        elif st[i - 1] == upper[i - 1]:
            d = -1 if c[i] > dn else 1
        else:
            d = 1 if c[i] < up else -1
        direction[i] = d
        st[i] = up if d == -1 else dn
    return {"supertrend": st, "direction": direction, "upper": upper, "lower": lower}


def _walk(n=400, base=100.0, seed=7, drift=0.0, vol=1.0):
    rng = random.Random(seed)
    h, l, c = [], [], []
    px = base
    for _ in range(n):
        px += rng.gauss(drift, vol)
        o = px
        hi = o + abs(rng.gauss(0, vol * 0.6))
        lo = o - abs(rng.gauss(0, vol * 0.6))
        c.append(o)
        h.append(hi)
        l.append(lo)
    return h, l, c


class TestPineFidelity(unittest.TestCase):
    def _cmp(self, h, l, c, period=10, mult=3.0, msg=""):
        got = supertrend(h, l, c, period, mult)
        exp = ref_supertrend(h, l, c, period, mult)
        for key in ("supertrend", "direction", "upper", "lower"):
            for i in range(len(c)):
                g, e = got[key][i], exp[key][i]
                if e is None:
                    self.assertIsNone(g, f"{msg} {key}[{i}] 应为 None")
                    continue
                self.assertIsNotNone(g, f"{msg} {key}[{i}] 不应为 None")
                self.assertAlmostEqual(
                    float(g), float(e), places=9, msg=f"{msg} {key}[{i}]"
                )

    def test_matches_reference_uptrend(self):
        h, l, c = _walk(300, seed=11, drift=0.35, vol=1.0)
        self._cmp(h, l, c, msg="uptrend")

    def test_matches_reference_downtrend(self):
        h, l, c = _walk(300, seed=12, drift=-0.35, vol=1.0)
        self._cmp(h, l, c, msg="downtrend")

    def test_matches_reference_chop(self):
        h, l, c = _walk(500, seed=13, drift=0.0, vol=1.5)
        self._cmp(h, l, c, msg="chop")

    def test_matches_reference_other_params(self):
        h, l, c = _walk(300, seed=14, vol=2.0)
        for period, mult in ((7, 2.0), (14, 4.0), (20, 1.5)):
            self._cmp(h, l, c, period, mult, msg=f"p{period}_m{mult}")

    def test_matches_reference_hi_vol_eth_like(self):
        h, l, c = _walk(400, base=3000.0, seed=15, vol=18.0)
        self._cmp(h, l, c, msg="eth_like")


class TestOrientationInvariants(unittest.TestCase):
    """方向与线必须自洽 —— 这正是原实现的硬伤。"""

    def test_direction_matches_band_side(self):
        h, l, c = _walk(400, seed=21, vol=1.4)
        r = supertrend(h, l, c, 10, 3.0)
        for i in range(len(c)):
            d, st, up, lo = (
                r["direction"][i], r["supertrend"][i], r["upper"][i], r["lower"][i]
            )
            if d is None:
                continue
            self.assertIn(d, (-1, 1))
            if d == -1:              # uptrend → 线在下轨（支撑）
                self.assertAlmostEqual(st, lo, places=9, msg=f"bar{i} d=-1 应为下轨")
            else:                    # downtrend → 线在上轨（压力）
                self.assertAlmostEqual(st, up, places=9, msg=f"bar{i} d=1 应为上轨")

    def test_first_bar_is_downtrend_with_upper_band(self):
        h, l, c = _walk(120, seed=22)
        r = supertrend(h, l, c, 10, 3.0)
        first = next(i for i, v in enumerate(r["supertrend"]) if v is not None)
        self.assertEqual(r["direction"][first], 1)
        self.assertAlmostEqual(r["supertrend"][first], r["upper"][first], places=9)

    def test_clean_uptrend_ends_in_uptrend_below_price(self):
        # 单调上涨：方向必然是 uptrend，线（下轨）必须在最后几根收盘价之下
        n = 120
        c = [100.0 + i * 0.8 for i in range(n)]
        h = [x + 0.3 for x in c]
        l = [x - 0.3 for x in c]
        r = supertrend(h, l, c, 10, 3.0)
        self.assertEqual(r["direction"][-1], -1)
        self.assertLess(r["supertrend"][-1], c[-1])

    def test_clean_downtrend_ends_in_downtrend_above_price(self):
        n = 120
        c = [200.0 - i * 0.8 for i in range(n)]
        h = [x + 0.3 for x in c]
        l = [x - 0.3 for x in c]
        r = supertrend(h, l, c, 10, 3.0)
        self.assertEqual(r["direction"][-1], 1)
        self.assertGreater(r["supertrend"][-1], c[-1])

    def test_flips_happen(self):
        h, l, c = _walk(600, seed=23, vol=1.6)
        r = supertrend(h, l, c, 10, 3.0)
        dirs = [d for d in r["direction"] if d is not None]
        flips = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)
        self.assertGreater(flips, 3, "震荡序列应当出现多次翻转")

    def test_band_monotonicity_while_trending(self):
        """趋势中 band 只能朝有利方向走（Pine 的 max/min 锁定）。"""
        h, l, c = _walk(300, seed=24, drift=0.4, vol=1.0)
        r = supertrend(h, l, c, 10, 3.0)
        for i in range(1, len(c)):
            if r["direction"][i] == -1 and r["direction"][i - 1] == -1:
                self.assertGreaterEqual(
                    r["lower"][i], r["lower"][i - 1] - 1e-9,
                    f"上涨中下轨不应下移 bar{i}",
                )
            if r["direction"][i] == 1 and r["direction"][i - 1] == 1:
                self.assertLessEqual(
                    r["upper"][i], r["upper"][i - 1] + 1e-9,
                    f"下跌中上轨不应上移 bar{i}",
                )

    def test_short_series_is_all_none(self):
        h, l, c = [1.0] * 5, [0.5] * 5, [0.8] * 5
        r = supertrend(h, l, c, 10, 3.0)
        self.assertTrue(all(v is None for v in r["supertrend"]))


class TestToolNames(unittest.TestCase):
    """字段必须能从工具名取到 —— 否则模型只能拿线，拿不到方向。"""

    def test_line_names(self):
        for name, period, mult in (
            ("supertrend", 10, 3.0),
            ("supertrend10", 10, 3.0),
            ("supertrend7_2", 7, 2.0),
            ("supertrend14_4.5", 14, 4.5),
        ):
            spec = parse_indicator_name(name)
            self.assertEqual(spec["kind"], "supertrend", name)
            self.assertEqual(spec["period"], period, name)
            self.assertAlmostEqual(spec["mult"], mult, msg=name)
            self.assertEqual(spec["field"], "supertrend", name)

    def test_direction_names(self):
        for name in ("supertrend_dir", "supertrend10_dir", "supertrend10_direction"):
            spec = parse_indicator_name(name)
            self.assertEqual(spec["field"], "direction", name)

    def test_band_names(self):
        self.assertEqual(parse_indicator_name("supertrend_upper")["field"], "upper")
        self.assertEqual(parse_indicator_name("supertrend10_up")["field"], "upper")
        self.assertEqual(parse_indicator_name("supertrend_lower")["field"], "lower")
        self.assertEqual(parse_indicator_name("supertrend10_lo")["field"], "lower")


class TestAttachAndLatest(unittest.TestCase):
    def test_attach_direction_and_bands(self):
        h, l, c = _walk(120, seed=31, drift=0.3)
        rows = [{"t": i, "o": c[i], "h": h[i], "l": l[i], "c": c[i], "v": 1.0}
                for i in range(len(c))]
        wanted = ["supertrend10", "supertrend10_dir", "supertrend10_upper",
                  "supertrend10_lower"]
        attach_indicators(rows, wanted)
        last = latest_indicators(rows, wanted)
        for k in wanted:
            self.assertIsNotNone(last[k], k)
        self.assertIn(last["supertrend10_dir"], (-1, 1))
        # 自洽：dir=-1 → 线 == 下轨
        if last["supertrend10_dir"] == -1:
            self.assertAlmostEqual(last["supertrend10"], last["supertrend10_lower"], places=9)
        else:
            self.assertAlmostEqual(last["supertrend10"], last["supertrend10_upper"], places=9)

    def test_direction_series_in_rows(self):
        h, l, c = _walk(80, seed=32, vol=1.2)
        rows = [{"t": i, "o": c[i], "h": h[i], "l": l[i], "c": c[i], "v": 1.0}
                for i in range(len(c))]
        attach_indicators(rows, ["supertrend10_dir"])
        vals = [r["supertrend10_dir"] for r in rows if r.get("supertrend10_dir") is not None]
        self.assertTrue(vals)
        self.assertTrue(all(v in (-1, 1) for v in vals))


if __name__ == "__main__":
    unittest.main()
