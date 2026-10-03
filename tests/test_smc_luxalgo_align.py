# -*- coding: utf-8 -*-
"""smc_map / smc_events 与 LuxAlgo 原版 Pine 语义的对齐测试。

每个用例锁定一条原版语义，避免再次漂移。重点覆盖三类历史缺陷：
  1. `leg()` 写成「当前 bar 创 size 根新高」而不是回顾式枢轴 → 窄幅震荡下 0 枢轴
  2. `trailing` / Premium-Discount 用全历史极值而不是最后 swing 枢轴
  3. OB 高度依赖 ATR，而 ATR 周期(200) 超过数据长度 → 全部塌成 0

两类数据形状各有用途：
  - `wave`（衰减锯齿）：峰递减、谷递增。「当前 bar 创 size 根新高」永不成立，
    而「size 根前是局部高点」成立 —— 用来区分两种 leg 语义。
  - `rnd_walk`（固定种子随机游走）：会产生枢轴，价格也会穿越枢轴，
    用来验证 BOS/CHoCH、OB 生成与 mitigation。种子 27 已校准满足全部断言。
"""

import random
import unittest

from omnialpha.strategist.indicators import _resolve_wanted
from omnialpha.strategist.smc_events import compute_smc_events
from omnialpha.strategist.smc_map import (
    _atr_series,
    _leg_series,
    compute_smc_map,
)
from omnialpha.strategist.tools import _strip_carried_indicators


def bars_from(highs, lows, closes):
    """`o` 取前一根收盘（与 `c` 不同），贴近真实 OHLC。"""
    out = []
    for i in range(len(highs)):
        o = closes[i - 1] if i else closes[i]
        out.append({"t": i * 300, "o": o,
                    "h": max(highs[i], o, closes[i]),
                    "l": min(lows[i], o, closes[i]),
                    "c": closes[i], "v": 100.0})
    return out


def wave(n=200, lo=90.0, hi=110.0, leg_len=20, decay=0.04):
    """衰减锯齿：峰逐次降低、谷逐次抬高（见模块 docstring）。"""
    vals = []
    k = 0
    while len(vals) < n:
        up = (k % 2 == 0)
        a_hi = hi - (hi - lo) * decay * k
        a_lo = lo + (hi - lo) * decay * k
        for j in range(leg_len):
            if len(vals) >= n:
                break
            if up:
                vals.append(a_lo + (a_hi - a_lo) * (j + 1) / leg_len)
            else:
                vals.append(a_hi - (a_hi - a_lo) * (j + 1) / leg_len)
        k += 1
    return bars_from([v + 0.5 for v in vals], [v - 0.5 for v in vals], vals)


def rnd_walk(n=200, seed=27, start=100.0, vol=2.0):
    """固定种子随机游走 —— 确定性，且会穿越枢轴。"""
    rng = random.Random(seed)
    closes = []
    v = start
    for _ in range(n):
        v += rng.uniform(-vol, vol)
        closes.append(v)
    highs = [c + rng.uniform(0.2, 1.5) for c in closes]
    lows = [c - rng.uniform(0.2, 1.5) for c in closes]
    return bars_from(highs, lows, closes)


# 精确构造：bar5..9 是低点(80)，bar10 是高点(120)，size=5 时
#   bar14 → lows[9]=80 < min(lows[10..14])  → leg 转 BULLISH → 低点枢轴 @ bar9
#   bar15 → highs[10]=120 > max(highs[11..15]) → leg 转 BEARISH → 高点枢轴 @ bar10
_PIVOT_HIGHS = [100.0] * 5 + [100.0] * 5 + [120.0] + [100.0] * 5
_PIVOT_LOWS = [99.0] * 5 + [80.0] * 5 + [119.0] + [99.0] * 5


def _old_breakout_leg(highs, lows, size):
    """曾经的错误实现：检测「当前 bar 创 size 根新高」而非回顾式枢轴。"""
    n = len(highs)
    leg = [0] * n
    state = 0
    for i in range(n):
        if i < size:
            leg[i] = state
            continue
        if highs[i] > max(highs[i - size: i]):
            state = 0
        elif lows[i] < min(lows[i - size: i]):
            state = 1
        leg[i] = state
    return leg


class TestLegSemantics(unittest.TestCase):
    def test_leg_is_lookback_pivot_not_breakout(self):
        leg = _leg_series(_PIVOT_HIGHS, _PIVOT_LOWS, 5)
        self.assertEqual(leg[14], 1, "lows[9]=80 低于其后 5 根 → 应转 BULLISH_LEG")
        self.assertEqual(leg[15], 0, "highs[10]=120 高于其后 5 根 → 应转 BEARISH_LEG")

    def test_old_breakout_semantics_would_find_nothing(self):
        """同一份数据下，旧的「突破」语义一个枢轴都不产生 —— 修复前的实况。"""
        bars = wave(200)
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        old = _old_breakout_leg(highs, lows, 50)
        self.assertEqual(len(set(old)), 1, "旧实现全程同一状态 → 0 个枢轴")
        self.assertGreater(len(set(_leg_series(highs, lows, 50))), 1,
                           "新实现应产生枢轴")

    def test_pivot_bar_index_is_detection_bar_minus_size(self):
        # 刻意构造的 h/l 不能被 bars_from 的 o/c 覆盖修正，这里自建
        mid = [(h + l) / 2 for h, l in zip(_PIVOT_HIGHS, _PIVOT_LOWS)]
        bars = [{"t": i * 300, "o": mid[i], "h": _PIVOT_HIGHS[i], "l": _PIVOT_LOWS[i],
                 "c": mid[i], "v": 100.0} for i in range(len(mid))]
        res = compute_smc_map(bars, swings_length=5, internal_size=3, eqh_len=3)
        found = {(p["bar"], p["kind"]): p["price"] for p in res.swing_pivots}
        self.assertEqual(found.get((9, "low")), 80.0)
        self.assertEqual(found.get((10, "high")), 120.0)


class TestTrailingAndPremiumDiscount(unittest.TestCase):
    def test_trailing_comes_from_last_swing_pivot(self):
        res = compute_smc_map(rnd_walk())
        highs = [p["price"] for p in res.swing_pivots if p["kind"] == "high"]
        lows = [p["price"] for p in res.swing_pivots if p["kind"] == "low"]
        self.assertTrue(highs and lows, "随机游走应产生 swing 枢轴")
        self.assertEqual(res.trailing["top"], highs[-1])
        self.assertEqual(res.trailing["bottom"], lows[-1])

    def test_premium_discount_uses_swing_range_not_full_history(self):
        bars = rnd_walk()
        res = compute_smc_map(bars)
        pd = res.premium_discount
        self.assertEqual(pd["source"], "swing_range")
        t, b = res.trailing["top"], res.trailing["bottom"]
        self.assertAlmostEqual(pd["equilibrium"], (t + b) / 2)
        self.assertAlmostEqual(pd["premium_top"], max(t, b))
        self.assertAlmostEqual(pd["discount_bottom"], min(t, b))
        full = max(x["h"] for x in bars) - min(x["l"] for x in bars)
        self.assertLess(pd["premium_top"] - pd["discount_bottom"], full,
                        "区间必须窄于全历史 —— 说明没有用 max(highs)/min(lows)")

    def test_current_zone_relative_to_equilibrium(self):
        bars = rnd_walk()
        res = compute_smc_map(bars)
        eq = res.premium_discount["equilibrium"]
        self.assertEqual(res.premium_discount["current_zone"],
                         "premium" if bars[-1]["c"] > eq else "discount")


class TestOrderBlocks(unittest.TestCase):
    def test_ob_height_non_zero(self):
        res = compute_smc_map(rnd_walk())
        obs = res.swing_order_blocks + res.internal_order_blocks
        self.assertTrue(obs, "应产生 OB")
        for o in obs:
            self.assertGreater(abs(o.bar_high - o.bar_low), 0, "OB 不得退化成一条线")

    def test_ob_count_respects_limit(self):
        res = compute_smc_map(rnd_walk(400), ob_limit=2, internal_ob_limit=3)
        self.assertLessEqual(len(res.swing_order_blocks), 2)
        self.assertLessEqual(len(res.internal_order_blocks), 3)

    def test_ob_slice_excludes_trigger_bar(self):
        """原版 slice(pivotBarIndex, bar_index) 左闭右开。"""
        res = compute_smc_map(rnd_walk())
        struct = [e for e in res.events
                  if e["type"] in ("swing_bos", "swing_choch",
                                   "internal_bos", "internal_choch")]
        self.assertTrue(struct)
        last = struct[-1]["bar_index"]
        for o in res.swing_order_blocks + res.internal_order_blocks:
            self.assertLessEqual(o.bar_index, last)

    def test_ob_mitigation_runs_every_bar(self):
        """逐 bar 检查：被吃掉的 OB 不留在列表，事件 bar 分布在全区间。"""
        res = compute_smc_map(rnd_walk())
        alive = res.swing_order_blocks + res.internal_order_blocks
        self.assertTrue(all(o.mitigated_bar is None for o in alive))
        events = [e for e in res.events if e["type"] == "ob_mitigated"]
        self.assertTrue(events, "数据应出现 OB mitigation")
        self.assertLess(min(e["bar_index"] for e in events), 199,
                        "不应只在最后一根 bar 检查")

    def test_mitigation_source_close_vs_highlow(self):
        bars = rnd_walk()
        hl = compute_smc_map(bars, ob_mitigation="highlow")
        cl = compute_smc_map(bars, ob_mitigation="close")
        n_hl = len([e for e in hl.events if e["type"] == "ob_mitigated"])
        n_cl = len([e for e in cl.events if e["type"] == "ob_mitigated"])
        self.assertGreaterEqual(n_hl, n_cl, "High/Low 比 Close 更容易触及 OB")


class TestStructureEvents(unittest.TestCase):
    def test_event_level_matches_a_real_pivot(self):
        res = compute_smc_map(rnd_walk())
        piv_prices = {round(p["price"], 6) for p in res.swing_pivots}
        struct = [e for e in res.events if e["type"].startswith("swing_")]
        self.assertTrue(struct, "应出现 swing 级结构事件")
        for e in struct:
            self.assertIn(round(e["level"], 6), piv_prices,
                          "swing 结构事件的 level 必须来自真实 swing 枢轴")

    def test_events_have_valid_tags_and_bars(self):
        res = compute_smc_map(rnd_walk())
        seq = [e for e in res.events
               if e["type"] in ("swing_bos", "swing_choch",
                                "internal_bos", "internal_choch")]
        self.assertTrue(seq)
        for e in seq:
            self.assertIn(e["tag"], ("BOS", "CHoCH"))
            self.assertGreater(e["bar_index"], 0)
            self.assertIsInstance(e["level"], float)


class TestAtrAdaptation(unittest.TestCase):
    def test_atr_adapts_when_history_shorter_than_period(self):
        bars = rnd_walk(80)
        atr, used, degraded = _atr_series(
            [b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars], 200)
        self.assertEqual(used, 80, "周期应收缩到数据长度")
        self.assertTrue(degraded)
        self.assertTrue(all(a is not None and a > 0 for a in atr), "不得留 None")

    def test_atr_matches_pine_when_enough_history(self):
        bars = rnd_walk(300)
        atr, used, degraded = _atr_series(
            [b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars], 200)
        self.assertEqual(used, 200)
        self.assertFalse(degraded, "数据充足时不得降级，逐值与原版一致")
        self.assertIsNone(atr[197], "Pine ta.atr(200) 前 199 根为 na")
        self.assertIsNotNone(atr[199])

    def test_structure_scale_exposes_volatility(self):
        """工具必须自带波动率尺度，模型才不必去调 indicators 找 ATR。"""
        sc = compute_smc_map(rnd_walk()).structure_scale
        for key in ("atr", "atr_period_used", "swing_range",
                    "recent_bar_range_avg", "recent_bars"):
            self.assertIn(key, sc)
        self.assertGreater(sc["atr"], 0)
        self.assertGreater(sc["swing_range"], 0)
        self.assertGreater(sc["recent_bar_range_avg"], 0)


class TestFvg(unittest.TestCase):
    def test_fvg_auto_threshold_filters(self):
        bars = rnd_walk(300)
        loose = compute_smc_map(bars, fvg_auto_threshold=False)
        tight = compute_smc_map(bars, fvg_auto_threshold=True)
        self.assertGreaterEqual(len(loose.fvgs), len(tight.fvgs))

    def test_fvg_mitigated_when_price_crosses_back(self):
        res = compute_smc_map(rnd_walk())
        mit = [e for e in res.events if e["type"] == "fvg_mitigated"]
        self.assertTrue(mit, "数据应出现 FVG 回填")
        self.assertTrue(all(f.mitigated_bar is None for f in res.fvgs),
                        "已被回填的 FVG 不应留在存活列表")


class TestKlinesLeak(unittest.TestCase):
    ROW = {"t": 1, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0,
           "ema20": "84583.09806979", "atr14": "17.98579215"}

    def test_explicit_empty_indicators_strips_carried_columns(self):
        out = _strip_carried_indicators(dict(self.ROW), _resolve_wanted([]))
        self.assertEqual(sorted(out), ["c", "h", "l", "o", "t", "v"])

    def test_unset_indicators_keeps_default_columns(self):
        out = _strip_carried_indicators(dict(self.ROW), _resolve_wanted(None))
        self.assertIn("ema20", out)
        self.assertIn("atr14", out)

    def test_partial_whitelist_keeps_only_requested(self):
        out = _strip_carried_indicators(dict(self.ROW), _resolve_wanted(["ema20"]))
        self.assertIn("ema20", out)
        self.assertNotIn("atr14", out)


class TestSmcEventsObHeight(unittest.TestCase):
    def test_ob_has_height_with_short_history(self):
        """atr_period 默认 200，但只喂 120 根 —— 修复前 OB 高度全为 0。"""
        res = compute_smc_events(rnd_walk(120), ms_len=5)
        obs = res.bull_obs + res.bear_obs
        self.assertTrue(obs, "应产生 OB")
        for o in obs:
            self.assertGreater(abs(o.top - o.btm), 0, "OB 不得退化成一条线")

    def test_ob_height_stable_across_lengths(self):
        for n in (100, 150, 300):
            res = compute_smc_events(rnd_walk(n), ms_len=5)
            obs = res.bull_obs + res.bear_obs
            if not obs:
                continue
            zero = [o for o in obs if abs(o.top - o.btm) < 1e-9]
            self.assertEqual(zero, [], f"n={n} 出现零高度 OB")


if __name__ == "__main__":
    unittest.main()
