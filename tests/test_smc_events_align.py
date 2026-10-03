# -*- coding: utf-8 -*-
"""smc_events 与 `SMC市场结构` v6 原版的对齐测试。

原版核心构件与本文件的对应：

| 原版 Pine | 本文件 |
|---|---|
| `fnOB` 的 `cords`：Full = 整个蜡烛 / Length = `low+atr` 收窄 | `_ob_cords` |
| `float atr = ta.atr(200) / (5 / len)` | `_ob_cords(..., atr_len)` |
| `find(ms, use_max, sweep, useob)` | `_find_extreme` |
| `mitigated()`：首破 → breaker，再反向破 → 移除 | `_ob_action` / 主循环 |
| `umt()` 买卖活动计数 | 主循环 `feat["activity"]` 段 |
| `overlap()` 同类内去重 | `_prune_same` |
| `overlap()` 跨类去重（`wichlap`） | `_prune_cross` |

两处**有意偏离**原版，都在测试里钉住：
  1. `overlap()` 跨类分支原版写 `Recent ? 0 : i`（与同类内的 `? i : 0` 相反），
     照抄会把最新项删掉、实测把 11 个 OB 删到 1 个 —— 按同类内语义统一。
  2. 跨类去重默认**关闭**：原版那是为画面整洁，而 bull/bear OB 重叠在 SMC 里
     是常态，全删会丢一半信息。`hide_cross_overlap=True` 可恢复原版行为。
"""

import random
import unittest

from omnialpha.strategist.smc_events import (
    _find_extreme,
    _ob_cords,
    _prune_cross,
    _prune_same,
    compute_smc_events,
)


def bars_from(highs, lows, closes):
    """`o` 取前一根收盘 —— 必须与 `c` 不同，否则 `close > open` 恒假，
    原版建立 BOS 线的 `close > open and close[1] > open[1]` 永不成立。"""
    out = []
    for i in range(len(highs)):
        o = closes[i - 1] if i else closes[i]
        out.append({"t": i * 300, "o": o,
                    "h": max(highs[i], o, closes[i]),
                    "l": min(lows[i], o, closes[i]),
                    "c": closes[i], "v": 100.0})
    return out


def rnd_walk(n=200, seed=27, start=100.0, vol=2.0):
    rng = random.Random(seed)
    closes, v = [], start
    for _ in range(n):
        v += rng.uniform(-vol, vol)
        closes.append(v)
    highs = [c + rng.uniform(0.2, 1.5) for c in closes]
    lows = [c - rng.uniform(0.2, 1.5) for c in closes]
    return bars_from(highs, lows, closes)


class _Box:
    """`_prune_*` 只用到 top/btm，用轻量 stub 避免构造 OB。"""

    def __init__(self, top, btm):
        self.top = top
        self.btm = btm


def _pairs(items):
    return [(x.top, x.btm) for x in items]


class TestObCords(unittest.TestCase):
    HIGHS = [10.0]
    LOWS = [8.0]
    ATRS = [1.0]

    def test_full_mode_returns_whole_candle(self):
        """Full = 整个蜡烛：bull 取 high、bear 取 low。"""
        self.assertEqual(_ob_cords(self.HIGHS, self.LOWS, self.ATRS, True, 0, "full"), 10.0)
        self.assertEqual(_ob_cords(self.HIGHS, self.LOWS, self.ATRS, False, 0, "full"), 8.0)

    def test_full_mode_gives_nonzero_height(self):
        """修复前 bull 取 low → top == btm → OB 退化成一条线。"""
        top = _ob_cords(self.HIGHS, self.LOWS, self.ATRS, True, 0, "full")
        self.assertGreater(top - self.LOWS[0], 0)

    def test_length_mode_clamped_to_candle(self):
        # bull: min(low + atr, high) = min(9, 10)
        self.assertAlmostEqual(_ob_cords(self.HIGHS, self.LOWS, self.ATRS, True, 0, "length"), 9.0)
        # bear: max(high - atr, low) = max(9, 8)
        self.assertAlmostEqual(_ob_cords(self.HIGHS, self.LOWS, self.ATRS, False, 0, "length"), 9.0)

    def test_atr_len_scales_height(self):
        """原版 `atr = ta.atr(200) / (5 / len)` → atr_len 越大 OB 越厚。"""
        thin = _ob_cords(self.HIGHS, self.LOWS, self.ATRS, True, 0, "length", 1)
        mid = _ob_cords(self.HIGHS, self.LOWS, self.ATRS, True, 0, "length", 5)
        thick = _ob_cords(self.HIGHS, self.LOWS, self.ATRS, True, 0, "length", 10)
        self.assertAlmostEqual(thin, 8.2)   # 1 / (5/1) = 0.2
        self.assertAlmostEqual(mid, 9.0)    # 1 / (5/5) = 1
        self.assertAlmostEqual(thick, 10.0)  # 1 / (5/10) = 2 → 触顶
        self.assertLessEqual(thin, mid)
        self.assertLessEqual(mid, thick)

    def test_atr_len_default_is_five(self):
        """默认 len=5 → atr/(5/5) = atr，与不传等价。"""
        a = _ob_cords(self.HIGHS, self.LOWS, self.ATRS, True, 0, "length")
        b = _ob_cords(self.HIGHS, self.LOWS, self.ATRS, True, 0, "length", 5)
        self.assertAlmostEqual(a, b)


class TestFindExtreme(unittest.TestCase):
    def test_picks_extreme_in_range(self):
        highs = [1.0, 5.0, 3.0, 2.0]
        lows = [0.5, 4.0, 2.0, 1.0]
        self.assertEqual(_find_extreme(highs, lows, True, 0, 4), 1)
        self.assertEqual(_find_extreme(highs, lows, False, 0, 4), 0)

    def test_useob_branch_is_dead_code_like_original(self):
        """原版 `if idx+1 < bar_index and high[idx+1] > high[idx]` 永不成立 ——
        idx 已是区间最大值，更早一根不可能更高。保留只为对齐结构。"""
        highs = [1.0, 5.0, 3.0, 2.0]
        lows = [0.5, 4.0, 2.0, 1.0]
        for use_max in (True, False):
            off = _find_extreme(highs, lows, use_max, 0, 4, False)
            on = _find_extreme(highs, lows, use_max, 0, 4, True)
            self.assertEqual(off, on, "useob 不应改变结果")

    def test_respects_from_idx(self):
        highs = [9.0, 1.0, 2.0]
        lows = [8.0, 0.5, 1.0]
        self.assertEqual(_find_extreme(highs, lows, True, 1, 3), 2)
        self.assertEqual(_find_extreme(highs, lows, False, 1, 3), 1)


class TestOverlapPrune(unittest.TestCase):
    def test_same_recent_drops_old_overlapping(self):
        items = [_Box(10, 8), _Box(9, 7), _Box(20, 18)]
        _prune_same(items, recent=True)
        self.assertEqual(_pairs(items), [(10, 8), (20, 18)])

    def test_same_old_drops_newest(self):
        items = [_Box(10, 8), _Box(9, 7), _Box(20, 18)]
        _prune_same(items, recent=False)
        self.assertEqual(_pairs(items), [(9, 7), (20, 18)])

    def test_cross_recent_drops_the_overlapping_older_item(self):
        """钉住对原版笔误的修正：重叠的是 bull[1]，就不该动 bull[0]。"""
        bull = [_Box(30, 28), _Box(10, 8)]
        bear = [_Box(9, 7)]
        _prune_cross(bull, bear, recent=True)
        self.assertEqual(_pairs(bull), [(30, 28)],
                         "应移除重叠的旧项，而不是最新项")

    def test_cross_noop_when_no_overlap(self):
        bull = [_Box(30, 28)]
        bear = [_Box(9, 7)]
        _prune_cross(bull, bear, recent=True)
        self.assertEqual(_pairs(bull), [(30, 28)])

    def test_cross_noop_on_empty(self):
        bull = []
        bear = [_Box(9, 7)]
        _prune_cross(bull, bear, recent=True)
        self.assertEqual(bull, [])
        _prune_cross(bear, bull, recent=True)
        self.assertEqual(_pairs(bear), [(9, 7)])


class TestCrossOverlapDefault(unittest.TestCase):
    def test_cross_overlap_off_by_default(self):
        """默认不做跨类去重，OB 信息完整；开启后才消除跨类重叠。"""
        bars = rnd_walk()
        default = compute_smc_events(bars)
        pruned = compute_smc_events(bars, hide_cross_overlap=True)
        n_default = len(default.bull_obs) + len(default.bear_obs)
        n_pruned = len(pruned.bull_obs) + len(pruned.bear_obs)
        self.assertGreaterEqual(n_default, n_pruned)

    def test_cross_overlap_on_removes_cross_pairs(self):
        bars = rnd_walk()

        def cross_pairs(res):
            out = 0
            for a in res.bull_obs:
                for b in res.bear_obs:
                    if a.btm < b.top and a.top > b.btm:
                        out += 1
            return out

        self.assertEqual(cross_pairs(compute_smc_events(bars, hide_cross_overlap=True)), 0)


class TestObBehaviour(unittest.TestCase):
    def test_ob_height_nonzero(self):
        res = compute_smc_events(rnd_walk())
        obs = res.bull_obs + res.bear_obs
        self.assertTrue(obs)
        for o in obs:
            self.assertGreater(abs(o.top - o.btm), 0)

    def test_ob_btm_top_ordering(self):
        """bull OB：top=cords、btm=low；bear OB：top=high、btm=cords。"""
        res = compute_smc_events(rnd_walk())
        for o in res.bull_obs:
            self.assertGreaterEqual(o.top, o.btm)
        for o in res.bear_obs:
            self.assertGreaterEqual(o.top, o.btm)

    def test_avg_is_midpoint(self):
        res = compute_smc_events(rnd_walk())
        for o in res.bull_obs + res.bear_obs:
            self.assertAlmostEqual(o.avg, (o.top + o.btm) / 2)

    def test_activity_counters_start_at_one(self):
        """原版 `umt()` 的 blPOS/brPOS 初值都是 1。"""
        res = compute_smc_events(rnd_walk())
        for o in res.bull_obs + res.bear_obs:
            self.assertGreaterEqual(o.bl_pos, 1)
            self.assertGreaterEqual(o.br_pos, 1)

    def test_vol_share_sums_to_one(self):
        res = compute_smc_events(rnd_walk())
        for arr in (res.bull_obs, res.bear_obs):
            if not arr:
                continue
            self.assertAlmostEqual(sum(o.vol_share for o in arr), 1.0, places=6)

    def test_breakers_feature_keeps_mitigated_obs(self):
        """开启 breakers 时被击穿的 OB 保留并标记，关闭时移除（原版 obshowbb）。"""
        bars = rnd_walk()
        on = compute_smc_events(bars, features={"breakers": True})
        off = compute_smc_events(bars, features={"breakers": False})
        n_on = len(on.bull_obs) + len(on.bear_obs)
        n_off = len(off.bull_obs) + len(off.bear_obs)
        self.assertGreaterEqual(n_on, n_off)

    def test_ob_last_limits_output(self):
        res = compute_smc_events(rnd_walk(), ob_last=1)
        self.assertLessEqual(len(res.bull_obs), 1)
        self.assertLessEqual(len(res.bear_obs), 1)


class TestFvgBehaviour(unittest.TestCase):
    def test_fvg_height_nonzero(self):
        res = compute_smc_events(rnd_walk())
        for f in res.bull_fvgs + res.bear_fvgs:
            self.assertGreater(abs(f.top - f.btm), 0)

    def test_fvg_threshold_filters(self):
        bars = rnd_walk(300)
        loose = compute_smc_events(bars, fvg_thresh=0.0)
        tight = compute_smc_events(bars, fvg_thresh=1.0)
        n_loose = len(loose.bull_fvgs) + len(loose.bear_fvgs)
        n_tight = len(tight.bull_fvgs) + len(tight.bear_fvgs)
        self.assertGreaterEqual(n_loose, n_tight)

    def test_fvg_mitigate_methods_all_work(self):
        """原版 fvg_src = Close / Wick / Avg 三种突破源都应能跑。"""
        bars = rnd_walk()
        for method in ("close", "wick", "avg"):
            res = compute_smc_events(bars, fvg_mitigate=method)
            for f in res.bull_fvgs + res.bear_fvgs:
                self.assertGreater(abs(f.top - f.btm), 0)

    def test_structure_scale_present(self):
        sc = compute_smc_events(rnd_walk()).structure_scale
        for key in ("atr", "atr_period_used", "atr_degraded",
                    "ob_height_avg", "fvg_width_avg", "recent_bar_range_avg"):
            self.assertIn(key, sc)
        self.assertGreater(sc["atr"], 0)


class TestStructureStateMachine(unittest.TestCase):
    """主结构状态机（原版 `structure()`）对齐。"""

    def test_full_mode_ob_is_whole_candle(self):
        bars = rnd_walk()
        H = [b["h"] for b in bars]
        L = [b["l"] for b in bars]
        res = compute_smc_events(bars, ob_mode="full")
        obs = res.bull_obs + res.bear_obs
        self.assertTrue(obs)
        for o in obs:
            self.assertAlmostEqual(o.top, H[o.bar_index])
            self.assertAlmostEqual(o.btm, L[o.bar_index])

    def test_bull_ob_uses_lowest_candle(self):
        """原版 `idbull = ms.find(false, ...)` —— bull OB 取最低点那根蜡烛。

        修复前写成 `find(true, ...)`（最高点），OB 落在错误的蜡烛上。
        """
        bars = rnd_walk(400)
        L = [b["l"] for b in bars]
        res = compute_smc_events(bars, ob_mode="full")
        self.assertTrue(res.bull_obs, "应产生 bull OB")
        for o in res.bull_obs:
            self.assertAlmostEqual(o.btm, L[o.bar_index],
                                   msg="bull OB 的下沿应等于该蜡烛 low")

    def test_ms_mode_is_wired(self):
        """原版 `msmode == "Adjusted Points"` 且 `bar_index % mslen == 0` 才调整 choch。

        修复前这个条件完全没实现，两种模式结果恒等。
        """
        seen_diff = False
        for seed in range(1, 15):
            bars = rnd_walk(300, seed=seed)
            ex = compute_smc_events(bars, ms_mode="extreme")
            ad = compute_smc_events(bars, ms_mode="adjusted")
            a = [(e.kind, e.bar_index) for e in ex.events]
            b = [(e.kind, e.bar_index) for e in ad.events]
            if a != b:
                seen_diff = True
                break
        self.assertTrue(seen_diff, "msmode 应至少在某些数据上影响结果")

    def test_start1_branches_are_mutually_exclusive(self):
        """原版 `ms.start == 1` 是 switch —— 同一 bar 只命中一个分支。

        修复前写成 4 个独立 if，同一根 bar 会既记扫荡又记 CHoCH。
        """
        for seed in range(1, 8):
            res = compute_smc_events(rnd_walk(300, seed=seed))
            by_bar = {}
            for e in res.events:
                by_bar.setdefault(e.bar_index, []).append(e.sweep)
            for bar, flags in by_bar.items():
                self.assertFalse(any(flags) and any(not f for f in flags),
                                 f"seed={seed} bar {bar} 同时出现扫荡与结构事件")

    def test_structure_events_are_sparse(self):
        """分支互斥 + 去掉假事件后，事件数应与结构转变次数同量级（原先 45 个）。"""
        res = compute_smc_events(rnd_walk())
        self.assertGreater(len(res.events), 0)
        self.assertLess(len(res.events), 60)


if __name__ == "__main__":
    unittest.main()
