# -*- coding: utf-8 -*-
"""tv_wyckoff 与 Wyckoff [theUltimator5] 原版 Pine v6 的对齐测试。

锁定六类原版语义：

  1. **双评分表**：`f_structureConfidence` 的 8 项权重（10/15/10/15/15/15/10/10）
     与 `f_validationScore` 的 10 项权重。后者声明了 `_accept` 却**从不使用** ——
     这是原版行为，测试专门钉住它（免得被「顺手补上」）。
  2. **稳健区间边界** `f_robustEdge`：三枢轴**中位数**，单点离群不生效；
     低点取 max、高点取 min（往保守一侧收）。
  3. **枢轴等值口径**：窗口内出现等值 bar 即不算枢轴（严格比较），
     与仓库既有 `smc_events.pivothigh` 一致。
  4. **na 传播**：`_le/_ge/_lt/_gt` 任一侧为 na 即为 False（Pine 语义），
     而不是 Python 的 `None` 比较异常。
  5. **阶段门槛函数**的夹取行为（`f_phaseBMinBars` 夹在 [30, 60] 等）。
  6. **状态机整体**：人工积累形态必须依次推进 A→B→C→D→E，且事件含
     SC/ST/Spring/SOS/Markup；`phase=None`（无活跃战役）是合法状态。

运行：`.venv\\Scripts\\python.exe -m unittest tests.test_tv_wyckoff -v`
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.tv_indicators import wyckoff as W  # noqa: E402
from omnialpha.strategist.tv_tools import (  # noqa: E402
    TV_TOOL_DEFS,
    TV_TOOL_NAMES,
    run_tv_tool,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def accumulation_bars(seed: int = 11, jitter: float = 0.06):
    """人工积累形态：下跌 → SC → AR → 区间 → Spring → SOS → markup。

    high/low 上加一点抖动，避免出现**完全等值**的 K 线——真实 OHLC 里极少
    精确等值，而本模块用严格口径判定枢轴（等值会取消枢轴资格）。
    """
    rng = random.Random(seed)
    bars = []

    def bar(o, h, l, c, v):
        j = lambda: rng.uniform(-jitter, jitter)  # noqa: E731
        bars.append((o + j(), h + j(), l + j(), c + j(), v * rng.uniform(0.94, 1.06)))

    px = 100.0
    for _ in range(150):                       # 下跌趋势
        px -= 0.10
        bar(px + 0.2, px + 0.4, px - 0.4, px - 0.2, 1000.0)
    bar(88.0, 88.5, 80.0, 86.2, 7000.0)        # SC：量大、价差宽、创新低、收在上部
    for k in range(1, 21):                     # AR：反弹
        px = 86.0 + k * 0.32
        bar(px - 0.3, px + 0.35, px - 0.45, px, 1900.0)
    for i in range(150):                       # 区间震荡，量递减
        ph = (i % 25) / 25.0
        px = 88.5 - ph * 2 * 7.5 if ph < 0.5 else 81.0 + (ph - 0.5) * 2 * 7.5
        bar(px - 0.1, px + 0.45, px - 0.45, px + 0.1, 1300.0 - i * 2.5)
    bar(81.2, 81.4, 79.7, 84.6, 950.0)         # Spring：跌破区间低点后收回
    for i in range(12):                        # 安静重测
        px = 84.6 - (i % 4) * 0.35
        bar(px + 0.1, px + 0.35, px - 0.35, px, 800.0 - i * 15)
    bar(88.4, 92.6, 88.3, 92.2, 5200.0)        # SOS：放量收在区间上方
    for k in range(1, 46):                     # markup
        px = 92.2 + k * 0.28
        bar(px - 0.25, px + 0.4, px - 0.45, px, 2100.0)
    return bars


def distribution_bars(seed: int = 21, jitter: float = 0.06, base=200.0):
    """积累形态的镜像：上涨 → BC → AR 下行 → 区间 → UTAD → SOW → markdown。"""
    return _mirror(accumulation_bars(seed, jitter), base)


def _mirror(bars, base=200.0):
    """把价格镜像（p → base - p），方向整体翻转。"""
    out = []
    for o, h, l, c, v in bars:
        out.append((base - o, base - l, base - h, base - c, v))
    return out


def pack(bars, t0=1700000000, step=900):
    """时间戳单位是 **epoch 秒**（与 Gate REST / 本地 kline.db 一致）。"""
    return ([b[0] for b in bars], [b[1] for b in bars], [b[2] for b in bars],
            [b[3] for b in bars], [b[4] for b in bars],
            [t0 + i * step for i in range(len(bars))])


def run_engine(bars, **kw):
    o, h, l, c, v, t = pack(bars)
    return W.compute_wyckoff(o, h, l, c, v, times=t, **kw)


def run_engine_prefix(bars, k, **kw):
    o, h, l, c, v, t = pack(bars)
    return W.compute_wyckoff(o[:k], h[:k], l[:k], c[:k], v[:k], times=t[:k], **kw)


def fresh_state():
    return W._WS()


def seeded_state(side=W.DIR_ACCUM, score=6, p_len=4, trend=-30.0):
    s = fresh_state()
    W._seed(s, side, score, p_len, trend, False, False, 1.5, 1200.0, 2.0,
            i=100, t=1700000000.0,
            low_i=80.0, high_i=85.0)
    return s


# ---------------------------------------------------------------------------
# 1. 小工具与 na 语义
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    def test_nz_default_zero(self):
        self.assertEqual(W._nz(None), 0.0)
        self.assertEqual(W._nz(3.5), 3.5)

    def test_nz_with_default(self):
        self.assertEqual(W._nz(None, 7.0), 7.0)
        self.assertEqual(W._nz(2.0, 7.0), 2.0)

    def test_nzv_keeps_na_of_other(self):
        # Pine nz(x, y) 在 x 为 na 时返回 y，而 y 本身也可能是 na
        self.assertIsNone(W._nzv(None, None))
        self.assertEqual(W._nzv(None, 5.0), 5.0)
        self.assertEqual(W._nzv(1.0, 5.0), 1.0)

    def test_clamp(self):
        self.assertEqual(W._clamp(5.0, 0.0, 3.0), 3.0)
        self.assertEqual(W._clamp(-5.0, 0.0, 3.0), 0.0)
        self.assertEqual(W._clamp(1.0, 0.0, 3.0), 1.0)

    def test_bits(self):
        self.assertFalse(W._has_bit(0, W.BIT_SC))
        m = W._add_bit(0, W.BIT_SC)
        self.assertTrue(W._has_bit(m, W.BIT_SC))
        # 幂等：重复加同一 bit 不改变
        self.assertEqual(W._add_bit(m, W.BIT_SC), m)
        m = W._add_bit(m, W.BIT_ST)
        self.assertTrue(W._has_bit(m, W.BIT_SC))
        self.assertTrue(W._has_bit(m, W.BIT_ST))
        self.assertFalse(W._has_bit(m, W.BIT_SOS))

    def test_pine_na_comparisons_are_false(self):
        # Pine 里 `na <= 1` 是 false，不是异常
        self.assertFalse(W._le(None, 1.0))
        self.assertFalse(W._le(1.0, None))
        self.assertFalse(W._ge(None, 1.0))
        self.assertFalse(W._lt(None, 1.0))
        self.assertFalse(W._gt(None, 1.0))
        self.assertTrue(W._le(1.0, 1.0))
        self.assertFalse(W._lt(1.0, 1.0))
        self.assertTrue(W._gt(2.0, 1.0))

    def test_off_propagates_na(self):
        self.assertIsNone(W._off(None, 1.0, 2.0))
        self.assertIsNone(W._off(1.0, 1.0, None))
        self.assertAlmostEqual(W._off(100.0, -0.5, 2.0), 99.0)

    def test_iso_treats_input_as_epoch_seconds(self):
        # 本仓库 K 线 t 的单位是秒；按毫秒解释会得到 1970 年
        self.assertEqual(W._iso(1700000000.0), "2023-11-14 22:13:20")
        self.assertEqual(W._iso(1791108900.0), "2026-10-04 10:15:00")
        self.assertIsNone(W._iso(None))

    def test_tf_secs_median(self):
        t = [0.0, 900.0, 1800.0, 2700.0]
        self.assertEqual(W._tf_secs(t), 900.0)
        self.assertEqual(W._tf_secs([0.0, 3600.0, 7200.0]), 3600.0)

    def test_tf_secs_ignores_gaps_and_falls_back(self):
        # 非单调/重复的时间戳被过滤；不足两条时回退 900
        self.assertEqual(W._tf_secs([0.0]), 900.0)
        self.assertEqual(W._tf_secs([]), 900.0)
        self.assertEqual(W._tf_secs([5.0, 5.0, 5.0]), 900.0)


# ---------------------------------------------------------------------------
# 2. 双评分（原版权重表）
# ---------------------------------------------------------------------------

class TestStructureConfidence(unittest.TestCase):
    def test_all_true_is_100(self):
        self.assertEqual(
            W._structure_confidence(True, True, True, True, True, True, True, True), 100)

    def test_all_false_is_0(self):
        self.assertEqual(
            W._structure_confidence(False, False, False, False, False, False, False, False), 0)

    def test_individual_weights(self):
        def sc(**kw):
            flags = dict(prior=False, climax=False, ar=False, st=False, c_done=False,
                         strength=False, lps=False, accept=False)
            flags.update(kw)
            return W._structure_confidence(
                flags["prior"], flags["climax"], flags["ar"], flags["st"],
                flags["c_done"], flags["strength"], flags["lps"], flags["accept"])

        self.assertEqual(sc(prior=True), 10)
        self.assertEqual(sc(climax=True), 15)
        self.assertEqual(sc(ar=True), 10)
        self.assertEqual(sc(st=True), 15)
        self.assertEqual(sc(c_done=True), 15)
        self.assertEqual(sc(strength=True), 15)
        self.assertEqual(sc(lps=True), 10)
        self.assertEqual(sc(accept=True), 10)
        self.assertEqual(sc(prior=True, climax=True), 25)


class TestValidationScore(unittest.TestCase):
    def _v(self, **kw):
        base = dict(trend=0.0, absorbed=False, range_atr=0.0, st=0, opp=0, trav=0,
                    clean=False, cause=0.0, terminal=False, strength=False,
                    lps=False, accept=False)
        base.update(kw)
        return W._validation_score(
            base["trend"], base["absorbed"], base["range_atr"], base["st"],
            base["opp"], base["trav"], base["clean"], base["cause"],
            base["terminal"], base["strength"], base["lps"], base["accept"])

    def test_all_strong_is_100(self):
        v = self._v(trend=-40.0, absorbed=True, range_atr=3.0, st=2, opp=1, trav=2,
                    clean=True, cause=3.0, terminal=True, strength=True, lps=True)
        self.assertEqual(v, 100)

    def test_accept_flag_is_ignored_by_original(self):
        """原版声明了 `_accept` 但从不使用——这里钉住这个原版行为。"""
        a = self._v(absorbed=True, accept=True)
        b = self._v(absorbed=True, accept=False)
        self.assertEqual(a, b)
        self.assertEqual(a, 15)

    def test_trend_bands(self):
        self.assertEqual(self._v(trend=W.priorTrendStrong), 10)
        self.assertEqual(self._v(trend=W.priorTrendNeutral), 5)
        self.assertEqual(self._v(trend=W.priorTrendNeutral - 0.1), 0)
        # 用绝对值：强下跌同样给 10
        self.assertEqual(self._v(trend=-W.priorTrendStrong), 10)

    def test_range_band_full_and_partial(self):
        self.assertEqual(self._v(range_atr=W.phaseBRangeMinATR), 10)
        self.assertEqual(self._v(range_atr=W.phaseBRangeMaxATR), 10)
        # 落在放宽带（0.75x ~ 1.5x）给 5
        self.assertEqual(self._v(range_atr=W.phaseBRangeMinATR * 0.75), 5)
        self.assertEqual(self._v(range_atr=W.phaseBRangeMaxATR * 1.5), 5)
        self.assertEqual(self._v(range_atr=0.1), 0)

    def test_steps(self):
        self.assertEqual(self._v(st=W.minPhaseBTests), 15)
        self.assertEqual(self._v(st=1), 8)
        self.assertEqual(self._v(st=0), 0)

    def test_opp_and_traversal_combination(self):
        self.assertEqual(self._v(opp=1, trav=2), 15)
        self.assertEqual(self._v(opp=1, trav=1), 8)
        self.assertEqual(self._v(opp=0, trav=1), 8)
        self.assertEqual(self._v(opp=0, trav=0), 0)

    def test_cause_bands(self):
        self.assertEqual(self._v(cause=3.0), 10)
        self.assertEqual(self._v(cause=1.5), 5)
        self.assertEqual(self._v(cause=1.49), 0)

    def test_small_weights(self):
        self.assertEqual(self._v(strength=True), 3)
        self.assertEqual(self._v(lps=True), 2)
        self.assertEqual(self._v(clean=True), 10)
        self.assertEqual(self._v(terminal=True), 10)

    def test_never_exceeds_100(self):
        for _ in range(3):
            self.assertLessEqual(
                self._v(trend=99.0, absorbed=True, range_atr=3.0, st=9, opp=9,
                        trav=9, clean=True, cause=99.0, terminal=True,
                        strength=True, lps=True, accept=True), 100)


class TestCauseUnits(unittest.TestCase):
    def test_time_units_only(self):
        # b_age=24, p=4 → 24/24 = 1.0；range_atr 低于下限 → 无宽度奖励；trav=0
        self.assertAlmostEqual(W._cause_units(0, 24, 4, 1.0), 1.0)

    def test_width_bonus_and_traversals(self):
        # 1.0 + trav 2*0.5 + 0.5 = 2.5
        self.assertAlmostEqual(W._cause_units(2, 24, 4, 2.0), 2.5)

    def test_p_zero_does_not_divide_by_zero(self):
        self.assertAlmostEqual(W._cause_units(0, 5, 0, 0.0), 5.0)


class TestTestClass(unittest.TestCase):
    def test_not_near_is_none(self):
        self.assertEqual(W._test_class(False, True, True, 1.0, 1.0, 10.0, 10.0), W.TEST_NONE)

    def test_not_holding_is_failed(self):
        self.assertEqual(W._test_class(True, False, True, 1.0, 1.0, 10.0, 10.0), W.TEST_FAILED)

    def test_good_requires_quiet_effort_and_spread(self):
        self.assertEqual(W._test_class(True, True, True, 8.0, 8.0, 10.0, 10.0), W.TEST_GOOD)

    def test_poor_when_effort_too_high(self):
        # 9.1 > 10*0.90 → 不是 GOOD
        self.assertEqual(W._test_class(True, True, True, 9.1, 8.0, 10.0, 10.0), W.TEST_POOR)

    def test_poor_when_close_not_ok(self):
        self.assertEqual(W._test_class(True, True, False, 1.0, 1.0, 10.0, 10.0), W.TEST_POOR)

    def test_poor_when_climax_reference_is_na(self):
        # 原版里 `e <= na * x` 是 false → 只能落到 POOR
        self.assertEqual(W._test_class(True, True, True, 1.0, 1.0, None, 10.0), W.TEST_POOR)
        self.assertEqual(W._test_class(True, True, True, 1.0, 1.0, 10.0, None), W.TEST_POOR)


# ---------------------------------------------------------------------------
# 3. 阶段门槛函数（含夹取）
# ---------------------------------------------------------------------------

class TestPhaseThresholds(unittest.TestCase):
    def test_phase_a_min_bars(self):
        self.assertEqual(W._phase_a_min_bars(1), W.phaseAMinBars)
        self.assertEqual(W._phase_a_min_bars(3), W.phaseAMinBars)
        self.assertEqual(W._phase_a_min_bars(4), 8)

    def test_phase_b_min_bars_clamped(self):
        # raw = max(p*6, rangeATR*p)，再夹在 [minPhaseBBars, 2*minPhaseBBars]
        self.assertEqual(W._phase_b_min_bars(4, 2.0), W.minPhaseBBars)          # raw 24 → 抬到 30
        self.assertEqual(W._phase_b_min_bars(10, 2.0), W.minPhaseBBars * 2)     # raw 60 → 正好上限
        self.assertEqual(W._phase_b_min_bars(10, 20.0), W.minPhaseBBars * 2)    # raw 200 → 夹到 60

    def test_phase_c_to_d_min_bars(self):
        self.assertEqual(W._phase_c_to_d_min_bars(1), W.phaseCToDMinBars)
        self.assertEqual(W._phase_c_to_d_min_bars(4), W.phaseCToDMinBars)
        self.assertEqual(W._phase_c_to_d_min_bars(10), 5)

    def test_phase_d_min_bars(self):
        self.assertEqual(W._phase_d_min_bars(2), W.phaseDMinBars)
        self.assertEqual(W._phase_d_min_bars(10), 10)

    def test_excursion_recovery_limit(self):
        self.assertEqual(W._excursion_recovery_limit(1), W.excursionRecoveryBars)
        self.assertEqual(W._excursion_recovery_limit(4), 3)
        self.assertEqual(W._excursion_recovery_limit(8), 6)

    def test_phase_idle_limit_per_phase(self):
        self.assertEqual(W._phase_idle_limit(W.PHASE_A, True, 4),
                         max(W.phaseAAfterArMaxBars, 24))
        self.assertEqual(W._phase_idle_limit(W.PHASE_A, False, 4),
                         max(W.maxARBars, 16))
        self.assertEqual(W._phase_idle_limit(W.PHASE_B, False, 4),
                         max(W.phaseBIdleMaxBars, 80))
        self.assertEqual(W._phase_idle_limit(W.PHASE_C, False, 4),
                         max(W.phaseCIdleMaxBars, 32))
        self.assertEqual(W._phase_idle_limit(W.PHASE_D, False, 4),
                         max(W.phaseCIdleMaxBars * 2, 64))

    def test_phase_idle_limit_na_for_none_and_e(self):
        # 原版对 PHASE_NONE / PHASE_E 返回 na → 不触发「闲置淘汰」
        self.assertIsNone(W._phase_idle_limit(W.PHASE_NONE, False, 4))
        self.assertIsNone(W._phase_idle_limit(W.PHASE_E, False, 4))

    def test_phase_b_character_improving(self):
        # avg_e = 100, avg_s = 10；last 必须 <= avg_e 且 <= avg_s*1.05
        self.assertTrue(W._phase_b_character_improving(2, 200.0, 20.0, 90.0, 10.0))
        self.assertFalse(W._phase_b_character_improving(2, 200.0, 20.0, 110.0, 10.0))
        # spr 有 5% 容差
        self.assertTrue(W._phase_b_character_improving(2, 200.0, 20.0, 90.0, 10.4))
        self.assertFalse(W._phase_b_character_improving(2, 200.0, 20.0, 90.0, 11.0))

    def test_phase_b_character_needs_two_samples(self):
        self.assertFalse(W._phase_b_character_improving(1, 100.0, 10.0, 1.0, 1.0))
        self.assertFalse(W._phase_b_character_improving(2, 100.0, 10.0, None, 1.0))
        self.assertFalse(W._phase_b_character_improving(2, 100.0, 10.0, 1.0, None))


# ---------------------------------------------------------------------------
# 4. 稳健区间边界（三枢轴中位数）
# ---------------------------------------------------------------------------

class TestRobustEdge(unittest.TestCase):
    def test_three_pivots_uses_median(self):
        # 中位数是 81，既不是最小 80 也不是最大 82
        self.assertEqual(W._robust_edge(80.0, 81.0, 82.0, 79.0, True), 81.0)

    def test_single_outlier_pivot_does_not_win(self):
        # 离群的低枢轴 70 被中位数剔除
        self.assertEqual(W._robust_edge(70.0, 81.0, 82.0, 79.0, True), 81.0)
        # 离群的枢轴顺序无关
        self.assertEqual(W._robust_edge(82.0, 70.0, 81.0, 79.0, True), 81.0)

    def test_high_side_takes_min(self):
        self.assertEqual(W._robust_edge(80.0, 81.0, 82.0, 90.0, False), 81.0)

    def test_low_side_takes_max_conservative(self):
        # 中位数比原始边界更低时，低点取 max 保持原边界（不放松）
        self.assertEqual(W._robust_edge(78.0, 79.0, 80.0, 82.0, True), 82.0)

    def test_two_pivots_blends_with_original(self):
        # ((80+82)/2 + 79)/2 = 80 → max(79, 80) = 80
        self.assertEqual(W._robust_edge(80.0, 82.0, None, 79.0, True), 80.0)

    def test_one_pivot_blends_with_original(self):
        # (80 + 79)/2 = 79.5
        self.assertEqual(W._robust_edge(80.0, None, None, 79.0, True), 79.5)

    def test_no_pivot_returns_original(self):
        self.assertEqual(W._robust_edge(None, None, None, 79.0, True), 79.0)

    def test_only_first_argument_is_used_when_others_na(self):
        # e1 有值、e2/e3 为 na → 走「一个枢轴」分支（不是「两个」）
        self.assertEqual(W._robust_edge(80.0, None, 82.0, 79.0, True), 79.5)


# ---------------------------------------------------------------------------
# 5. 类型 / 偏向 / 硬位 / 区间高度
# ---------------------------------------------------------------------------

class TestTypeAndBias(unittest.TestCase):
    def test_type_from_accumulation(self):
        self.assertEqual(W._type_from(W.DIR_ACCUM, W.DIR_ACCUM, False, False), W.TYPE_ACCUM)
        # 停止侧是派发，或大周期看涨 → 再积累
        self.assertEqual(W._type_from(W.DIR_DIST, W.DIR_ACCUM, False, False), W.TYPE_REACCUM)
        self.assertEqual(W._type_from(W.DIR_ACCUM, W.DIR_ACCUM, True, False), W.TYPE_REACCUM)

    def test_type_from_distribution(self):
        self.assertEqual(W._type_from(W.DIR_DIST, W.DIR_DIST, False, False), W.TYPE_DIST)
        self.assertEqual(W._type_from(W.DIR_ACCUM, W.DIR_DIST, False, False), W.TYPE_REDIST)
        self.assertEqual(W._type_from(W.DIR_DIST, W.DIR_DIST, False, True), W.TYPE_REDIST)

    def test_type_from_none_outcome(self):
        self.assertEqual(W._type_from(W.DIR_ACCUM, W.DIR_NONE, False, False), W.TYPE_NONE)

    def test_bias_dir(self):
        s = fresh_state()
        self.assertEqual(W._bias_dir(s), W.DIR_NONE)
        s.stopSide = W.DIR_ACCUM
        self.assertEqual(W._bias_dir(s), W.DIR_ACCUM)
        s.ctxBear = True
        self.assertEqual(W._bias_dir(s), W.DIR_DIST)
        s2 = fresh_state()
        s2.stopSide = W.DIR_DIST
        self.assertEqual(W._bias_dir(s2), W.DIR_DIST)
        s2.ctxBull = True
        self.assertEqual(W._bias_dir(s2), W.DIR_ACCUM)

    def test_type_ws_uses_outcome_then_bias(self):
        s = fresh_state()
        self.assertEqual(W._type_ws(s), W.TYPE_NONE)
        s.stopSide = W.DIR_ACCUM
        # 尚未定局 → 用 bias（无上下文 → ACCUM）
        self.assertEqual(W._type_ws(s), W.TYPE_ACCUM)
        s.outcome = W.DIR_ACCUM
        self.assertEqual(W._type_ws(s), W.TYPE_ACCUM)

    def test_hard_level_prefers_excursion_extreme(self):
        s = seeded_state()
        # 无 exc：低点硬位 = min(origLow, rangeLow) = 80
        self.assertEqual(W._hard_level(s, W.DIR_ACCUM), 80.0)
        s.exc = True
        s.outcome = W.DIR_ACCUM
        s.excPrice = 79.0
        self.assertEqual(W._hard_level(s, W.DIR_ACCUM), 79.0)
        # outcome 不是 ACCUM 时不采用 excPrice
        s.outcome = W.DIR_NONE
        self.assertEqual(W._hard_level(s, W.DIR_ACCUM), 80.0)

    def test_hard_level_falls_back_to_range_edge(self):
        s = fresh_state()
        s.rangeLow = 70.0
        self.assertEqual(W._hard_level(s, W.DIR_ACCUM), 70.0)
        s.rangeHigh = 90.0
        self.assertEqual(W._hard_level(s, W.DIR_DIST), 90.0)

    def test_range_atr_ws(self):
        s = fresh_state()
        self.assertEqual(W._range_atr_ws(s, 2.0, 1e-9), 0.0)  # 无区间
        s.rangeHigh, s.rangeLow, s.climaxATR = 90.0, 80.0, 2.0
        self.assertAlmostEqual(W._range_atr_ws(s, 5.0, 1e-9), 5.0)
        # climaxATR 为 na 时退回当前 ATR
        s.climaxATR = None
        self.assertAlmostEqual(W._range_atr_ws(s, 5.0, 1e-9), 2.0)


# ---------------------------------------------------------------------------
# 6. 前序趋势评分
# ---------------------------------------------------------------------------

class TestPriorTrendScore(unittest.TestCase):
    def test_flat_market_is_zero(self):
        closes = [100.0] * 60
        zeros = [1.0] * 60
        none60 = [None] * 60
        got = W._prior_trend_score(closes, zeros, none60, none60, none60,
                                   i=59, n=20, end_off=2, mintick=1e-9)
        self.assertAlmostEqual(got, 0.0)

    def test_monotone_down_is_negative(self):
        closes = [200.0 - i for i in range(60)]
        ones = [1.0] * 60
        none60 = [None] * 60
        got = W._prior_trend_score(closes, ones, none60, none60, none60,
                                   i=59, n=20, end_off=2, mintick=1e-9)
        # disp 夹到 -3 → -45；er = 1 → -25；无枢轴、无长均线 → 合计 -70
        self.assertAlmostEqual(got, -70.0)

    def test_full_negative_is_clamped_at_minus_100(self):
        """位移 + 效率 + 摆动结构 + 长均线四项同时打到极值 → 恰好 -100。"""
        closes = [200.0 - i for i in range(60)]
        ones = [1.0] * 60
        sma_s = [200.0 - i for i in range(60)]          # 长均线下行 → -10
        ph = [None] * 60
        pl = [None] * 60
        for b, v in zip((40, 44, 48, 52, 56), (196.0, 192.0, 188.0, 184.0, 180.0)):
            ph[b] = v                                    # 下降的高点 → lh
        for b, v in zip((41, 45, 49, 53, 57), (159.0, 155.0, 151.0, 147.0, 143.0)):
            pl[b] = v                                    # 下降的低点 → ll
        got = W._prior_trend_score(closes, ones, ph, pl, sma_s,
                                   i=59, n=20, end_off=2, mintick=1e-9)
        self.assertAlmostEqual(got, -100.0)

    def test_insufficient_history_is_zero(self):
        closes = [100.0 - i for i in range(5)]
        ones = [1.0] * 5
        none5 = [None] * 5
        got = W._prior_trend_score(closes, ones, none5, none5, none5,
                                   i=4, n=20, end_off=2, mintick=1e-9)
        self.assertAlmostEqual(got, 0.0)


# ---------------------------------------------------------------------------
# 7. 枢轴（严格口径）
# ---------------------------------------------------------------------------

class TestPivotAt(unittest.TestCase):
    def test_plain_pivot_high(self):
        highs = [1.0, 5.0, 1.0]
        lows = [0.5, 4.0, 0.5]
        self.assertEqual(W._pivot_at(highs, lows, 2, 1, high=True), 5.0)

    def test_plain_pivot_low(self):
        highs = [5.0, 1.0, 5.0]
        lows = [4.0, 0.5, 4.0]
        self.assertEqual(W._pivot_at(highs, lows, 2, 1, high=False), 0.5)

    def test_tie_disqualifies_pivot_high(self):
        """窗口内出现等值 bar → 不算枢轴（本模块的口径，见模块 docstring）。"""
        highs = [1.0, 5.0, 5.0, 1.0]
        lows = [0.0, 4.0, 4.0, 0.0]
        self.assertIsNone(W._pivot_at(highs, lows, 2, 1, high=True))

    def test_tie_disqualifies_pivot_low(self):
        highs = [5.0, 1.0, 1.0, 5.0]
        lows = [4.0, 0.5, 0.5, 4.0]
        self.assertIsNone(W._pivot_at(highs, lows, 2, 1, high=False))

    def test_window_too_short_is_na(self):
        highs = [1.0, 5.0, 1.0]
        lows = [0.5, 4.0, 0.5]
        # i=1, p=1 → j=0, j-p<0 → na
        self.assertIsNone(W._pivot_at(highs, lows, 1, 1, high=True))

    def test_respects_pivot_length(self):
        highs = [1.0, 1.0, 3.0, 1.0, 1.0]
        lows = [0.5, 0.5, 2.0, 0.5, 0.5]
        self.assertEqual(W._pivot_at(highs, lows, 4, 2, high=True), 3.0)
        # p=1 时 j=3 不是枢轴（右侧无确认位）
        self.assertIsNone(W._pivot_at(highs, lows, 3, 2, high=True))

    def test_pivots_for_returns_both_sides(self):
        highs = [1.0, 5.0, 1.0]
        lows = [4.0, 0.5, 4.0]
        h, l = W._pivots_for(highs, lows, 2, 1)
        self.assertEqual((h, l), (5.0, 0.5))

    def test_zero_pivot_length_is_na(self):
        highs = [1.0, 5.0, 1.0]
        lows = [0.5, 4.0, 0.5]
        self.assertIsNone(W._pivot_at(highs, lows, 2, 0, high=True))


# ---------------------------------------------------------------------------
# 8. 战役级辅助函数
# ---------------------------------------------------------------------------

class TestCampaignHelpers(unittest.TestCase):
    def test_seed_accumulation_sets_phase_a_and_sc(self):
        s = seeded_state(W.DIR_ACCUM)
        self.assertEqual(s.phase, W.PHASE_A)
        self.assertEqual(s.stopSide, W.DIR_ACCUM)
        self.assertEqual(s.climaxPrice, 80.0)     # 低点侧
        self.assertEqual(s.origLow, 80.0)
        self.assertEqual(s.rangeLow, 80.0)
        self.assertIsNone(s.origHigh)
        self.assertTrue(s.cfPrior)
        self.assertEqual(s.ev, W.EV_SC)
        self.assertEqual(s.lastEventBar, 100)
        self.assertEqual(s.climaxScore, 6)

    def test_seed_distribution_sets_bc(self):
        s = seeded_state(W.DIR_DIST)
        self.assertEqual(s.climaxPrice, 85.0)     # 高点侧
        self.assertEqual(s.origHigh, 85.0)
        self.assertEqual(s.rangeHigh, 85.0)
        self.assertIsNone(s.origLow)
        self.assertEqual(s.ev, W.EV_BC)

    def test_freeze_ar_sets_opposite_edge(self):
        s = seeded_state(W.DIR_ACCUM)
        s.arRunPrice, s.arRunBar, s.arRunTime = 90.0, 110, 1700000100000.0
        W._freeze_ar(s)
        self.assertEqual(s.arBar, 110)
        self.assertEqual(s.arPrice, 90.0)
        self.assertTrue(s.cfAR)
        self.assertEqual(s.origHigh, 90.0)
        self.assertEqual(s.rangeHigh, 90.0)

    def test_register_st_accumulates_and_moves_edge(self):
        s = seeded_state(W.DIR_ACCUM)
        W._register_st(s, 81.0, 1700000300000.0, 120, 1000.0, 1.0, 5)
        self.assertEqual(s.stCount, 1)
        self.assertEqual(s.stN, 1)
        self.assertAlmostEqual(s.stEffSum, 1000.0)
        self.assertEqual(s.stBar, 120)
        self.assertEqual(s.stPrice, 81.0)
        self.assertEqual(s.stScore, 5)
        self.assertEqual(s.stHist.p1, 81.0)
        self.assertEqual(s.edge1, 81.0)
        self.assertTrue(s.cfST)
        self.assertEqual(s.ev, W.EV_ST)
        # 单枢轴 → (81+80)/2 = 80.5，低点取 max → 80.5
        self.assertAlmostEqual(s.rangeLow, 80.5)

    def test_register_st_three_pivots_uses_median(self):
        s = seeded_state(W.DIR_ACCUM)
        W._register_st(s, 81.0, 1.0, 120, 1000.0, 1.0, 5)
        W._register_st(s, 79.0, 2.0, 130, 900.0, 1.0, 5)
        self.assertEqual(s.edge2, 81.0)
        self.assertEqual(s.edge1, 79.0)
        # 两枢轴：((79+81)/2 + 80)/2 = 80 → max(80, 80) = 80
        self.assertAlmostEqual(s.rangeLow, 80.0)
        W._register_st(s, 78.0, 3.0, 140, 800.0, 1.0, 5)
        self.assertEqual(s.stCount, 3)
        self.assertEqual(s.stHist.p3, 78.0)
        # 三枢轴中位数 = 79，低于原边界 80 → 低点取 max 保持 80
        self.assertAlmostEqual(s.rangeLow, 80.0)

    def test_register_st_pivot_history_keeps_first_three(self):
        s = seeded_state(W.DIR_ACCUM)
        for k, pb in enumerate((120, 130, 140, 150), start=1):
            W._register_st(s, 80.0 + k, float(k), pb, 100.0, 1.0, 5)
        self.assertEqual(s.stCount, 4)
        self.assertEqual(s.stHist.p1, 81.0)
        self.assertEqual(s.stHist.p2, 82.0)
        self.assertEqual(s.stHist.p3, 83.0)      # 第 4 个不再写入

    def test_register_opp(self):
        s = seeded_state(W.DIR_ACCUM)
        W._register_opp(s, 89.0, 1700000400000.0, 125, 1200.0, 1.2, i=130)
        self.assertEqual(s.oppCount, 1)
        self.assertEqual(s.opN, 1)
        self.assertEqual(s.opLastBar, 125)
        self.assertEqual(s.opLastPrice, 89.0)
        self.assertEqual(s.opHist.p1, 89.0)
        self.assertEqual(s.lastActBar, 130)

    def test_clear_terminal(self):
        s = seeded_state(W.DIR_ACCUM)
        s.exc = True
        s.excTested = True
        s.excBar, s.excPrice, s.excTime = 120, 79.0, 1.0
        s.excEff, s.excSpr, s.excScore = 1.0, 1.0, 3
        s.testTime, s.testPrice, s.testScore = 1.0, 80.0, 2
        s.cfExc = True
        s.cfTest = True
        s.cReadyBar, s.cStartTime = 121, 1.0
        W._clear_terminal(s)
        self.assertFalse(s.exc)
        self.assertFalse(s.excTested)
        self.assertIsNone(s.excBar)
        self.assertIsNone(s.excPrice)
        self.assertEqual(s.excScore, 0)
        self.assertIsNone(s.testTime)
        self.assertEqual(s.testScore, 0)
        self.assertFalse(s.cfExc)
        self.assertFalse(s.cfTest)
        self.assertIsNone(s.cReadyBar)

    def test_promote_d_then_demote_to_b(self):
        s = seeded_state(W.DIR_ACCUM)
        W._promote_d(s, W.DIR_ACCUM, 4, 91.0, i=200, t=1700000500000.0, close_i=90.5)
        self.assertEqual(s.phase, W.PHASE_D)
        self.assertEqual(s.outcome, W.DIR_ACCUM)
        self.assertTrue(s.cfStrength)
        self.assertEqual(s.strPrice, 91.0)
        self.assertEqual(s.ev, W.EV_SOS)
        self.assertEqual(s.dStartBar, 200)

        W._demote_to_b(s, i=210)
        self.assertEqual(s.phase, W.PHASE_B)
        self.assertEqual(s.outcome, W.DIR_NONE)
        self.assertFalse(s.cfStrength)
        self.assertFalse(s.cfLPS)
        self.assertIsNone(s.strBar)
        self.assertEqual(s.lastBadBar, 210)

    def test_demote_to_b_clears_terminal(self):
        s = seeded_state(W.DIR_ACCUM)
        s.exc, s.excTested, s.cfTest = True, True, True
        W._demote_to_b(s, i=210)
        self.assertFalse(s.exc)
        self.assertFalse(s.cfTest)

    def test_conf_ws_cdone_needs_confirmed_excursion(self):
        """cDone = cfTest or (cfExc and cfStrength)。

        注意是 `cfExc`（已确认的探针）而不是 `exc`（探针本身）——
        只把 `exc` 置位并不满足 cDone。这是原版语义，测试钉住它。
        """
        s = seeded_state(W.DIR_ACCUM)
        W._promote_d(s, W.DIR_ACCUM, 4, 91.0, i=200, t=3.0, close_i=90.5)
        base = W._conf_ws(s)
        s.exc = True                      # 仅探针，未确认
        self.assertEqual(W._conf_ws(s), base)
        s.cfExc = True                    # 确认后才算 cDone
        self.assertEqual(W._conf_ws(s), base + 15)

    def test_conf_ws_grows_with_confirmations(self):
        s = seeded_state(W.DIR_ACCUM)                 # cfPrior
        self.assertEqual(W._conf_ws(s), 10)
        s.absorbed = True
        s.cfClimax = True
        self.assertEqual(W._conf_ws(s), 25)           # + climax 15
        s.arRunPrice, s.arRunBar, s.arRunTime = 90.0, 110, 1.0
        W._freeze_ar(s)
        self.assertEqual(W._conf_ws(s), 35)           # + ar 10
        W._register_st(s, 81.0, 2.0, 120, 1000.0, 1.0, 5)
        self.assertEqual(W._conf_ws(s), 50)           # + st 15
        W._promote_d(s, W.DIR_ACCUM, 4, 91.0, i=200, t=3.0, close_i=90.5)
        # cDone = cfTest or (cfExc and cfStrength)；只有 strength → 不满足 cDone
        self.assertEqual(W._conf_ws(s), 65)
        s.exc = True
        s.cfExc = True
        self.assertEqual(W._conf_ws(s), 80)           # + cDone 15
        s.cfLPS = True
        self.assertEqual(W._conf_ws(s), 90)           # + lps 10
        s.cfAccept = True
        self.assertEqual(W._conf_ws(s), 100)          # + accept 10

    def test_conf_ws_cdone_from_terminal_test(self):
        s = seeded_state(W.DIR_ACCUM)
        s.cfTest = True
        self.assertEqual(W._conf_ws(s), 25)           # prior 10 + cDone 15

    def test_validation_ws_zero_without_stop_side(self):
        s = fresh_state()
        self.assertEqual(W._validation_ws(s, 4, -30.0, 2.0, i=100, mintick=1e-9), 0)

    def test_entry_readiness_ws_bounds(self):
        s = seeded_state(W.DIR_ACCUM)
        lo = W._entry_readiness_ws(s, 60, 60, 2.0, i=200, close_i=85.0,
                                   t=1700000000.0, tf_secs=900.0, mintick=1e-9)
        self.assertGreaterEqual(lo, 0)
        self.assertLessEqual(lo, 9)

    def test_entry_readiness_ws_improves_when_matured(self):
        s = seeded_state(W.DIR_ACCUM)
        W._promote_d(s, W.DIR_ACCUM, 4, 91.0, i=200, t=1700000000.0, close_i=90.5)
        s.rangeHigh, s.rangeLow = 90.0, 80.0
        s.cfLPS = True
        s.lpsTime, s.lpsBar = 1700000000.0, 195
        hi = W._entry_readiness_ws(s, 60, 60, 2.0, i=200, close_i=91.0,
                                   t=1700000000.0, tf_secs=900.0, mintick=1e-9)
        s2 = seeded_state(W.DIR_ACCUM)
        lo = W._entry_readiness_ws(s2, 60, 60, 2.0, i=101, close_i=82.0,
                                   t=1700000000.0, tf_secs=900.0, mintick=1e-9)
        self.assertGreater(hi, lo)

    def test_entry_readiness_age_is_counted_in_bars(self):
        """年龄判据必须按 bar 数算：`tf_secs` 已是秒，不能再乘 1000。

        若误按毫秒处理（除数 *1000），100 根前的旧事件会被算成 0.1 根，
        「事件新鲜度」那一分就永远白送。
        """
        def readiness(bars_ago):
            s = seeded_state(W.DIR_ACCUM)
            W._promote_d(s, W.DIR_ACCUM, 4, 91.0, i=200, t=1700000000.0, close_i=90.5)
            s.rangeHigh, s.rangeLow = 90.0, 80.0
            s.cfLPS = True
            s.lpsTime, s.lpsBar = 1700000000.0, 195
            s.lastEventTime = 1700000000.0 - 900.0 * bars_ago
            return W._entry_readiness_ws(s, 60, 60, 2.0, i=200, close_i=91.0,
                                         t=1700000000.0, tf_secs=900.0, mintick=1e-9)

        fresh = readiness(10)    # 10 根前 → <= 50 → 计 1 分
        stale = readiness(100)   # 100 根前 → > 50 → 不计分
        self.assertEqual(fresh - stale, 1)


# ---------------------------------------------------------------------------
# 9. 引擎：契约与稳健性
# ---------------------------------------------------------------------------

class TestEngineContract(unittest.TestCase):
    def test_empty_series_is_error(self):
        self.assertIn("error", W.compute_wyckoff([], [], [], [], []))

    def test_length_mismatch_is_error(self):
        r = W.compute_wyckoff([1.0, 2.0], [1.0], [1.0, 2.0], [1.0, 2.0], [1.0, 2.0])
        self.assertIn("error", r)

    def test_output_schema(self):
        r = run_engine(accumulation_bars())
        for k in ("phase", "phase_name", "structure", "stop_side", "outcome",
                  "outcome_direction", "event", "confidence", "validation", "range",
                  "range_origin", "absorbed", "probe", "events", "entry", "next",
                  "checks", "atr", "trend_score"):
            self.assertIn(k, r)
        self.assertIn("high", r["range"])
        self.assertIn("low", r["range"])
        self.assertIn("height_atr", r["range"])
        self.assertIsInstance(r["events"], dict)
        self.assertIsInstance(r["checks"], list)
        self.assertIsInstance(r["next"], str)
        self.assertGreaterEqual(r["confidence"], 0)
        self.assertLessEqual(r["confidence"], 100)
        self.assertGreaterEqual(r["validation"], 0)
        self.assertLessEqual(r["validation"], 100)

    def test_checks_rows_shape(self):
        r = run_engine(accumulation_bars())
        self.assertTrue(r["checks"])
        for row in r["checks"]:
            self.assertEqual(set(row), {"name", "current", "required", "ok"})
            self.assertIsInstance(row["name"], str)
            self.assertIsInstance(row["ok"], bool)

    def test_determinism(self):
        bars = accumulation_bars()
        self.assertEqual(run_engine(bars), run_engine(bars))

    def test_invalid_strictness_falls_back(self):
        bars = accumulation_bars()
        self.assertEqual(run_engine(bars, strictness="Bogus"),
                         run_engine(bars, strictness="Standard"))

    def test_strictness_does_not_change_structure(self):
        """strictness 只影响入场触发，不影响阶段/结构判定。"""
        bars = accumulation_bars()
        a = run_engine(bars, strictness="Conservative")
        b = run_engine(bars, strictness="Aggressive")
        for k in ("phase", "structure", "stop_side", "outcome", "event",
                  "confidence", "validation"):
            self.assertEqual(a[k], b[k])

    def test_no_campaign_is_a_legit_state(self):
        """随机游走常常没有活跃战役；此时 phase 为 None 且 next 是搜索文案。"""
        rng = random.Random(7)
        bars = []
        px = 100.0
        for _ in range(600):
            px = max(1.0, px + rng.gauss(0, 0.8))
            hi, lo = px + 0.4, px - 0.4
            bars.append((px - 0.1, hi, lo, px, 1000.0))
        r = run_engine(bars)
        self.assertIsNone(r["phase"])
        self.assertEqual(r["next"], "Searching for SC / BC")
        self.assertEqual(r["confidence"], 0)
        self.assertEqual(r["events"], {})

    def test_short_series_does_not_crash(self):
        bars = accumulation_bars()[:40]
        r = run_engine(bars)
        self.assertNotIn("error", r)


# ---------------------------------------------------------------------------
# 10. 引擎：人工积累形态必须走完 A→B→C→D→E
# ---------------------------------------------------------------------------

class TestEngineAccumulation(unittest.TestCase):
    def test_final_state_is_phase_e_accumulation(self):
        r = run_engine(accumulation_bars())
        self.assertEqual(r["phase"], "E")
        self.assertEqual(r["structure"], "ACCUMULATION")
        self.assertEqual(r["stop_side"], "accum")
        self.assertEqual(r["outcome"], "accum")
        self.assertEqual(r["outcome_direction"], "up")
        self.assertEqual(r["event"], "Markup")
        self.assertTrue(r["absorbed"])
        self.assertTrue(r["entry"] is None or isinstance(r["entry"], dict))

    def test_final_state_stable_across_jitter_seeds(self):
        for seed in (11, 12, 13, 14):
            r = run_engine(accumulation_bars(seed))
            self.assertEqual(r["phase"], "E", f"seed={seed}")
            self.assertEqual(r["structure"], "ACCUMULATION", f"seed={seed}")

    def test_key_events_present(self):
        r = run_engine(accumulation_bars())
        ev = r["events"]
        self.assertIn("climax", ev)
        self.assertEqual(ev["climax"]["kind"], "SC")
        self.assertIn("st", ev)
        self.assertIn("spring", ev)
        self.assertIn("strength", ev)
        self.assertEqual(ev["strength"]["kind"], "SOS")
        for k in ("climax", "strength"):
            self.assertIsNotNone(ev[k]["time"])
            self.assertIsNotNone(ev[k]["price"])

    def test_range_is_sane(self):
        r = run_engine(accumulation_bars())
        hi, lo = r["range"]["high"], r["range"]["low"]
        self.assertIsNotNone(hi)
        self.assertIsNotNone(lo)
        self.assertGreater(hi, lo)
        self.assertGreater(r["range"]["height_atr"], 0.0)

    def test_all_phases_observed_over_prefixes(self):
        bars = accumulation_bars()
        seen = set()
        for k in range(150, len(bars) + 1, 5):
            seen.add(run_engine_prefix(bars, k)["phase"])
        for p in ("A", "B", "C", "D", "E"):
            self.assertIn(p, seen, f"阶段 {p} 从未出现")

    def test_next_text_never_empty(self):
        bars = accumulation_bars()
        for k in range(150, len(bars) + 1, 10):
            r = run_engine_prefix(bars, k)
            self.assertTrue(r["next"], f"k={k} 的 next 为空")

    def test_phase_a_next_mentions_stopping_action_or_ar(self):
        bars = accumulation_bars()
        seen = set()
        for k in range(150, 200):
            r = run_engine_prefix(bars, k)
            if r["phase"] == "A":
                seen.add(r["next"])
        self.assertTrue(seen, "Phase A 从未出现")
        joined = " | ".join(seen)
        self.assertTrue(
            "stopping action" in joined or "Waiting for AR" in joined,
            f"Phase A 的 next 文案不符预期: {joined}",
        )

    def test_distribution_mirror_is_detected(self):
        bars = distribution_bars()
        sides = set()
        for k in range(150, len(bars) + 1, 10):
            sides.add(run_engine_prefix(bars, k)["stop_side"])
        self.assertIn("dist", sides, "镜像形态未被识别为派发")

    def test_entry_signal_fires_on_mature_campaign(self):
        """入场信号必须真能触发——否则 `entry` 就是死字段。"""
        bars = accumulation_bars()
        o, h, l, c, v, t = pack(bars)
        found = None
        for k in range(150, len(bars) + 1):
            r = W.compute_wyckoff(o[:k], h[:k], l[:k], c[:k], v[:k], times=t[:k],
                                  strictness="Standard")
            if r["entry"] is not None:
                found = r
                break
        self.assertIsNotNone(found, "Standard 严格度下从未触发入场")
        self.assertIn(found["entry"]["kind"], ("Test", "SOS/SOW", "LPS/LPSY"))
        self.assertEqual(found["entry"]["strictness"], "Standard")
        self.assertIsNotNone(found["entry"]["time"])
        self.assertIsNotNone(found["entry"]["price"])
        # 入场时必然已进入 Phase C 及以后
        self.assertIn(found["phase"], ("C", "D", "E"))

    def test_conservative_entry_is_not_earlier_than_standard(self):
        """Conservative 的成熟度门槛更高，触发不得早于 Standard。"""
        bars = accumulation_bars()
        o, h, l, c, v, t = pack(bars)

        def first_entry_bar(strictness):
            for k in range(150, len(bars) + 1):
                r = W.compute_wyckoff(o[:k], h[:k], l[:k], c[:k], v[:k], times=t[:k],
                                      strictness=strictness)
                if r["entry"] is not None:
                    return k
            return None

        std = first_entry_bar("Standard")
        cons = first_entry_bar("Conservative")
        self.assertIsNotNone(std)
        self.assertTrue(cons is None or cons >= std,
                        f"Conservative 在 {cons} 早于 Standard 的 {std}")


# ---------------------------------------------------------------------------
# 11. 工具层
# ---------------------------------------------------------------------------

class _FakeRes:
    def __init__(self, rows):
        self.rows = rows
        self.source = "test"


def _rows_from(bars):
    o, h, l, c, v, t = pack(bars)
    return [{"t": t[i], "o": o[i], "h": h[i], "l": l[i], "c": c[i], "v": v[i]}
            for i in range(len(bars))]


class TestWyckoffTool(unittest.TestCase):
    def test_registered(self):
        names = [d["function"]["name"] for d in TV_TOOL_DEFS]
        self.assertIn("tv_wyckoff", names)
        self.assertIn("tv_wyckoff", TV_TOOL_NAMES)
        spec = next(d for d in TV_TOOL_DEFS if d["function"]["name"] == "tv_wyckoff")
        props = spec["function"]["parameters"]["properties"]
        self.assertIn("symbol", props)
        self.assertIn("tf", props)
        self.assertIn("limit", props)
        self.assertIn("entry_strictness", props)
        self.assertEqual(props["entry_strictness"]["enum"], list(W.STRICTNESS))

    def test_tool_returns_engine_snapshot(self):
        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(_rows_from(accumulation_bars()))):
            r = run_tv_tool(None, "tv_wyckoff",
                            {"symbol": "BTC_USDT", "tf": "15m"}, env="testnet",
                            bot_root=ROOT)
        self.assertEqual(r["symbol"], "BTC_USDT")
        self.assertEqual(r["tf"], "15m")
        self.assertEqual(r["bars"], len(accumulation_bars()))
        self.assertEqual(r["source"], "test")
        self.assertEqual(r["phase"], "E")
        self.assertEqual(r["structure"], "ACCUMULATION")
        self.assertIn("strength", r["events"])

    def test_limit_is_floored_to_500(self):
        seen = {}

        def fake(client, sym, tf, lim, **kw):
            seen["limit"] = lim
            return _FakeRes(_rows_from(accumulation_bars()))

        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles", side_effect=fake):
            run_tv_tool(None, "tv_wyckoff",
                        {"symbol": "BTC_USDT", "tf": "15m", "limit": 200},
                        env="testnet", bot_root=ROOT)
        self.assertEqual(seen["limit"], 500)

    def test_limit_above_floor_is_respected(self):
        seen = {}

        def fake(client, sym, tf, lim, **kw):
            seen["limit"] = lim
            return _FakeRes(_rows_from(accumulation_bars()))

        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles", side_effect=fake):
            run_tv_tool(None, "tv_wyckoff",
                        {"symbol": "BTC_USDT", "tf": "15m", "limit": 800},
                        env="testnet", bot_root=ROOT)
        self.assertEqual(seen["limit"], 800)

    def test_limit_capped_at_1500(self):
        seen = {}

        def fake(client, sym, tf, lim, **kw):
            seen["limit"] = lim
            return _FakeRes(_rows_from(accumulation_bars()))

        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles", side_effect=fake):
            run_tv_tool(None, "tv_wyckoff",
                        {"symbol": "BTC_USDT", "tf": "15m", "limit": 99999},
                        env="testnet", bot_root=ROOT)
        self.assertEqual(seen["limit"], 1500)

    def test_warmup_floor_makes_small_limit_irrelevant(self):
        """limit=200 与 limit=800 取到同一批数据时，结论必须一致。"""
        rows = _rows_from(accumulation_bars())
        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(rows)):
            a = run_tv_tool(None, "tv_wyckoff",
                            {"symbol": "BTC_USDT", "tf": "15m", "limit": 200},
                            env="testnet", bot_root=ROOT)
            b = run_tv_tool(None, "tv_wyckoff",
                            {"symbol": "BTC_USDT", "tf": "15m", "limit": 800},
                            env="testnet", bot_root=ROOT)
        self.assertEqual(a, b)

    def test_short_data_returns_error(self):
        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(_rows_from(accumulation_bars()[:50]))):
            r = run_tv_tool(None, "tv_wyckoff",
                            {"symbol": "BTC_USDT", "tf": "15m"}, env="testnet",
                            bot_root=ROOT)
        self.assertIn("error", r)

    def test_bad_strictness_does_not_error(self):
        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(_rows_from(accumulation_bars()))):
            r = run_tv_tool(None, "tv_wyckoff",
                            {"symbol": "BTC_USDT", "tf": "15m",
                             "entry_strictness": "Nope"}, env="testnet", bot_root=ROOT)
        self.assertNotIn("error", r)
        self.assertEqual(r["phase"], "E")

    def test_dispatched_via_run_tool(self):
        from omnialpha.strategist import tools as T

        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(_rows_from(accumulation_bars()))):
            r = T.run_tool(None, "tv_wyckoff", {"symbol": "BTC_USDT", "tf": "15m"},
                           env="testnet", bot_root=ROOT)
        self.assertIn("next", r)

    def test_tool_normalizes_millisecond_timestamps(self):
        """上游若给毫秒时间戳，工具就地归一到秒——结果必须与秒输入一致。"""
        bars = accumulation_bars()
        o, h, l, c, v, t = pack(bars)          # t 是秒
        rows_s = [{"t": t[i], "o": o[i], "h": h[i], "l": l[i], "c": c[i], "v": v[i]}
                  for i in range(len(bars))]
        rows_ms = [dict(r, t=r["t"] * 1000) for r in rows_s]

        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(rows_s)):
            a = run_tv_tool(None, "tv_wyckoff", {"symbol": "BTC_USDT", "tf": "15m"},
                            env="testnet", bot_root=ROOT)
        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(rows_ms)):
            b = run_tv_tool(None, "tv_wyckoff", {"symbol": "BTC_USDT", "tf": "15m"},
                            env="testnet", bot_root=ROOT)
        self.assertEqual(a, b)
        self.assertEqual(a["events"]["climax"]["time"][:4], "2023")


if __name__ == "__main__":
    unittest.main(verbosity=2)


