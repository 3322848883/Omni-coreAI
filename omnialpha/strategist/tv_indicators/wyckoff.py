"""Wyckoff [theUltimator5] —— Pine v6 原版状态机移植。

对齐源：TradingView 指标 `Wyckoff [theUltimator5]`（`//@version=6`，3204 行）。
本模块逐行翻译其计算核心，**不改判据、不调参数**：

| 本模块 | 原版 |
|---|---|
| 常量段 | 顶部 hard-coded settings（原版第 79–199 行） |
| `_WyckoffState` | `type WS`（原版第 367–484 行，~80 字段） |
| `_PivotTriplet` | `type PivotTriplet`（原版第 355 行） |
| `_prior_trend_score` | `f_priorTrendScore`（原版第 574 行） |
| `_structure_confidence` | `f_structureConfidence`（原版第 616 行） |
| `_validation_score` | `f_validationScore`（原版第 619 行） |
| `_robust_edge` | `f_robustEdge`（原版第 691 行，三枢轴中位数） |
| `_seed` / `_freeze_ar` / `_register_st` / `_register_opp` | 同名 `f_*`（原版第 732–831 行） |
| `_enter_ctest` / `_promote_d` / `_demote_to_b` | 同名 `f_*`（原版第 884–931 行） |
| `_describe` | `f_describe`（原版第 981 行，只保留 `next` 文本） |
| `compute_wyckoff` | `f_engine`（原版第 1061–1765 行） |

**有意不移植**（对齐方案 §2.2）：

- 参考示意图（`f_getSchematic` / `f_renderSchematic` / `f_renderOverlay` 等）—— 人类可视化
- 状态表 / 事件标签 / 历史快照（`HistSchem`）—— 同上
- `f_engineHTF` + `request.security` 多周期扫描 —— 原版用
  `lookahead = barmerge.lookahead_on`，有未来函数风险；改为让模型自己换 `tf` 多次调用
- `f_storeConfirmedEntry` / `EntrySignal` —— 去重与画图用；本模块只报当前入场状态

**相对方案的两处补充**（都是原版自带的数据，只是换了载体）：

1. 原版的**诊断表**（`f_describe` 的 `rows`）没有丢弃，而是作为结构化 `checks`
   输出（`{name, current, required, ok}`）。原版是画给人看的表格，这里改成数据。
2. `next` 字段保持原版 `nx` 的**逐字原文**，不做任何拼接。

**一个原版自身的特性**：`s.ev` 永远不会是 `AR`——原版 `f_freezeAR` 只置 `cfAR`
标志、不调 `f_setEvent`。AR 的时间/价格要从 `events.ar`（即 `s.arTime` /
`s.arPrice`）读，不要指望 `event` 字段报 AR。

**bar 语义**：原版 `f_engine(barstate.isconfirmed)` 只在**已收盘** bar 上改状态
（第 1796 行）。这里喂进来的 OHLCV 全部是已收盘 bar，故每根都提交，
等价于原版在每根收盘 bar 上跑一次。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..indicators import atr as _pine_atr
from ..indicators import highest as _pine_highest
from ..indicators import lowest as _pine_lowest
from ..indicators import sma as _pine_sma

# ---------------------------------------------------------------------------
# 参数表 —— 逐条对齐原版 hard-coded settings，**变量名保持不变**，
# 以便与 Pine 源码逐行对照（含原版行号）。改这里就等于改原版。
# ---------------------------------------------------------------------------

# Structure detection（原版 82–93 行）
pivotLen = 4
swingModeInput = "ATR Adaptive"
adaptiveSwingMin = 2
adaptiveSwingMax = 10
adaptiveSwingVolLen = 50
trendLen = 25
priorTrendMaxBars = 200
priorTrendPivotFactor = 8.0
priorTrendStrong = 35.0
priorTrendNeutral = 15.0
continuationTrendLen = 200
oppositeCycleMinBars = 25
extremeLen = 30

# Campaign lifecycle（原版 94–108 行）
phaseAMinBars = 6
phaseAAfterArMaxBars = 45
phaseBIdleMaxBars = 125
phaseCIdleMaxBars = 50
phaseAPrematureDepartureBars = 3
phaseAPrematureDepartureTRFrac = 0.30
phaseAPrematureDepartureATR = 0.75
structureInvalidationATR = 0.50
structureInvalidationBars = 2
phaseBDepartureBars = 3
phaseBDepartureTRFrac = 0.35
phaseBDepartureATR = 0.75
phaseDFailBars = 3
phaseEMaxBars = 300

# Volume and volatility（原版 110–121 行）
volLen = 50
atrLen = 14
climaxVolMult = 1.8
climaxSpreadMult = 1.5
relaxedClimaxFactor = 0.80
prelimVolMult = 1.25
prelimExtremeATR = 0.75
prelimEfficiencyLookback = 10
prelimEfficiencyRatio = 0.80
prelimMinScore = 4

# Climax confirmation（原版 123–131 行）
scCloseMin = 0.35
bcCloseMax = 0.65
climaxMinScore = 5
climaxAbsorptionMinBars = 3
climaxAbsorptionMaxBars = 8
climaxAbsorptionExtremeATR = 0.50
climaxAbsorptionReboundATR = 0.35
climaxAbsorptionVolFloor = 0.90

# AR and Phase B（原版 133–153 行）
minARATR = 2.0
maxARBars = 30
arMaxExtensionATR = 15.0
boundaryTolATR = 0.75
stMaxVolRatio = 0.90
stMaxSpreadRatio = 0.90
stMinScore = 4
minPhaseBBars = 30
minPhaseBTests = 2
minPhaseBOppositeTests = 1
minPhaseBTraversals = 2
phaseBZoneFrac = 0.25
phaseBRangeMinATR = 1.5
phaseBRangeMaxATR = 8.0
phaseBBadTestCooldownFactor = 2.0
phaseBEdgeExpansionCap = 0.15
terminalTestMinBarsAfterST = 3
terminalTestVolRatio = 0.85
terminalTestSpreadRatio = 0.85
terminalTestOtherMaxRatio = 1.05

# Phase C probes and tests（原版 155–167 行）
excursionRecoveryBars = 3
springMinPenATR = 0.15
springMaxPenATR = 2.5
springCloseMin = 0.55
utadCloseMax = 0.45
springEffortMaxMult = 1.50
excursionMinScore = 3
testTolATR = 1.25
testMaxVolRatio = 0.80
testMaxSpreadRatio = 0.80
testExtremeToleranceATR = 0.15
testMinScore = 2

# Phase D and E（原版 168–187 行）
phaseCToDMinBars = 3
phaseDMinBars = 4
phaseDValidationMin = 60
phaseDDominanceFrac = 0.65
breakATR = 0.15
strengthVolMult = 1.15
strengthSpreadATR = 1.15
sosCloseMin = 0.65
sowCloseMax = 0.35
strengthMinScore = 3
sosMultiBarLen = 5
sosMultiBarATR = 2.0
sosMultiBarEffort = 1.10
lpsBoundaryATR = 1.50
lpsVolMult = 1.00
lpsSpreadATR = 1.00
lpsMinScore = 2
confirmBars = 3
directAcceptanceBars = 5
directAcceptanceConfidence = 75
minConfidencePhaseC = 45
minConfidencePhaseD = 55

# Entries（原版 189–194 行）
entryMinConfidenceConservative = 75
entryMinConfidenceStandard = 60
entryMinConfidenceAggressive = 55
entryMinTestsConservative = 7
entryMinTestsStandard = 6
entryMinTestsAggressive = 5

# Core constants（原版 199–284 行）
DIR_NONE = 0
DIR_ACCUM = 1
DIR_DIST = -1

TYPE_NONE = 0
TYPE_ACCUM = 1
TYPE_REACCUM = 2
TYPE_DIST = -1
TYPE_REDIST = -2

REGIME_NONE = 0
REGIME_MARKUP = 1
REGIME_MARKDOWN = -1

PHASE_NONE = 0
PHASE_A = 1
PHASE_B = 2
PHASE_C = 3
PHASE_D = 4
PHASE_E = 5

EV_NONE = 0
EV_SC = 1
EV_BC = 2
EV_AR = 3
EV_ST = 4
EV_SPRING = 5
EV_UTAD = 6
EV_TEST = 7
EV_SOS = 8
EV_SOW = 9
EV_LPS = 10
EV_LPSY = 11
EV_CTEST_ACC = 12
EV_CTEST_DST = 13
EV_MARKUP = 14
EV_MARKDOWN = 15

TEST_NONE = 0
TEST_GOOD = 1
TEST_POOR = 2
TEST_FAILED = 3

ENTRY_NONE = 0
ENTRY_TEST = 1
ENTRY_STRENGTH = 2
ENTRY_LPS = 3
ENTRY_PHASE_E = 4

BIT_PS = 1
BIT_SC = 2
BIT_BC = 4
BIT_AR = 8
BIT_ST = 16
BIT_SPRING = 32
BIT_UTAD = 64
BIT_TEST = 128
BIT_SOS = 256
BIT_SOW = 512
BIT_LPS = 1024
BIT_LPSY = 2048
BIT_E = 4096
BIT_CTEST = 16384

RS_INVALID = 0
RS_ABSORB = 1
RS_NORANGE = 2
RS_DEPART = 3
RS_STALE = 4
RS_DEMOTE = 5
RS_EEND = 6

STRICTNESS = ("Conservative", "Standard", "Aggressive")

_PHASE_LETTER = {
    PHASE_NONE: None,
    PHASE_A: "A",
    PHASE_B: "B",
    PHASE_C: "C",
    PHASE_D: "D",
    PHASE_E: "E",
}

_PHASE_NAME = {
    PHASE_NONE: None,
    PHASE_A: "A - Stopping action",
    PHASE_B: "B - Building cause",
    PHASE_C: "C - Test",
    PHASE_D: "D - Trend within range",
    PHASE_E: "E - Trend out of range",
}

_EVENT_NAME = {
    EV_NONE: None,
    EV_SC: "SC",
    EV_BC: "BC",
    EV_AR: "AR",
    EV_ST: "ST",
    EV_SPRING: "Spring",
    EV_UTAD: "UTAD",
    EV_TEST: "Test",
    EV_SOS: "SOS",
    EV_SOW: "SOW",
    EV_LPS: "LPS",
    EV_LPSY: "LPSY",
    EV_CTEST_ACC: "Terminal test (support)",
    EV_CTEST_DST: "Terminal test (resistance)",
    EV_MARKUP: "Markup",
    EV_MARKDOWN: "Markdown",
}

_TYPE_NAME = {
    TYPE_NONE: None,
    TYPE_ACCUM: "ACCUMULATION",
    TYPE_REACCUM: "REACCUMULATION",
    TYPE_DIST: "DISTRIBUTION",
    TYPE_REDIST: "REDISTRIBUTION",
}

_ENTRY_KIND_NAME = {
    ENTRY_NONE: None,
    ENTRY_TEST: "Test",
    ENTRY_STRENGTH: "SOS/SOW",
    ENTRY_LPS: "LPS/LPSY",
    ENTRY_PHASE_E: "Phase E",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _nz(v: Optional[float], default: float = 0.0) -> float:
    """Pine `nz(x)` / `nz(x, y)`。"""
    return default if v is None else float(v)


def _nzv(v: Optional[float], other: Optional[float]) -> Optional[float]:
    """Pine `nz(x, y)` 保留 na 语义的版本（`other` 也允许是 na）。"""
    return other if v is None else v


def _clamp(v: float, lo: float, hi: float) -> float:
    """Pine `f_clamp`（原版第 562 行）。"""
    return max(lo, min(hi, v))


def _has_bit(mask: int, bit: int) -> bool:
    """Pine `f_hasBit`（原版第 565 行）。"""
    return int(mask / bit) % 2 == 1


def _add_bit(mask: int, bit: int) -> int:
    """Pine `f_addBit`（原版第 568 行）。"""
    return mask if _has_bit(mask, bit) else mask + bit


def _le(a: Optional[float], b: Optional[float]) -> bool:
    """Pine 的 `a <= b`：任一侧为 na 时结果为 false。"""
    return a is not None and b is not None and a <= b


def _ge(a: Optional[float], b: Optional[float]) -> bool:
    """Pine 的 `a >= b`：任一侧为 na 时结果为 false。"""
    return a is not None and b is not None and a >= b


def _lt(a: Optional[float], b: Optional[float]) -> bool:
    """Pine 的 `a < b`：任一侧为 na 时结果为 false。"""
    return a is not None and b is not None and a < b


def _gt(a: Optional[float], b: Optional[float]) -> bool:
    """Pine 的 `a > b`：任一侧为 na 时结果为 false。"""
    return a is not None and b is not None and a > b


def _at(seq: list, i: int, back: int = 0) -> Any:
    """Pine `series[back]`：取 `i - back`，越界返回 None（na）。"""
    j = i - back
    if j < 0 or j >= len(seq):
        return None
    return seq[j]


def _roll_sum(values: list[float], period: int) -> list[Optional[float]]:
    """Pine `math.sum`：滚动窗口求和。"""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0:
        return out
    run = 0.0
    for i in range(n):
        run += values[i]
        if i >= period:
            run -= values[i - period]
        if i + 1 >= period:
            out[i] = run
    return out


def _iso(secs: Optional[float]) -> Optional[str]:
    """epoch **秒** → ISO-8601 UTC 字符串。

    本仓库 K 线的 `t` 单位是**秒**（Gate REST 与本地 `kline.db` 一致），
    不是 Pine 的毫秒；故这里直接按秒解释，不做 /1000。
    """
    if secs is None:
        return None
    try:
        return datetime.fromtimestamp(float(secs), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _round(v: Optional[float], digits: int = 6) -> Optional[float]:
    return None if v is None else round(float(v), digits)


# ---------------------------------------------------------------------------
# 原版纯函数
# ---------------------------------------------------------------------------

def _prior_trend_score(
    closes: list[float],
    atr_s: list[Optional[float]],
    ph_s: list[Optional[float]],
    pl_s: list[Optional[float]],
    sma_s: list[Optional[float]],
    i: int,
    n: int,
    end_off: int,
    mintick: float,
) -> float:
    """`f_priorTrendScore`（原版第 574 行）。

    Pine 里 `close[e]` 是「当前 bar 往前 e 根」，故这里用 `i - e` 索引。
    评分 = 位移(±45) + 效率(±25) + 摆动结构(±20) + 长均线方向(±10)，夹在 ±100。
    """
    n = max(int(n), 2)
    e = max(int(end_off), 0)
    i_end = i - e
    i_start = i - e - n
    if i_end < 0 or i_start < 0:
        return 0.0
    c_end = closes[i_end]
    c_start = closes[i_start]
    if c_end is None or c_start is None:
        return 0.0

    unit_a = max(_nz(atr_s[i_end], _nz(atr_s[i], mintick)), mintick)
    net = c_end - c_start
    disp = _clamp(net / (unit_a * math.sqrt(n)), -3.0, 3.0) * 15.0

    path = 0.0
    hh = ll = lh = hl = 0
    last_ph: Optional[float] = None
    last_pl: Optional[float] = None
    for k in range(n - 1, -1, -1):
        o = e + k
        i_o = i - o
        if i_o < 0:
            continue
        c_o = closes[i_o]
        c_o1 = closes[i_o - 1] if i_o - 1 >= 0 else None
        path += abs(_nz(c_o) - _nz(c_o1, _nz(c_o)))
        vph = ph_s[i_o] if i_o < len(ph_s) else None
        vpl = pl_s[i_o] if i_o < len(pl_s) else None
        if vph is not None:
            if last_ph is not None:
                hh += 1 if vph > last_ph else 0
                lh += 1 if vph < last_ph else 0
            last_ph = vph
        if vpl is not None:
            if last_pl is not None:
                hl += 1 if vpl > last_pl else 0
                ll += 1 if vpl < last_pl else 0
            last_pl = vpl

    er = abs(net) / path if path > 0 else 0.0
    er_part = (1.0 if net >= 0 else -1.0) * er * 25.0
    swings = hh + ll + lh + hl
    swing_part = float(hh + hl - lh - ll) / swings * 20.0 if swings > 0 else 0.0

    sma_end = sma_s[i_end]
    sma_start = sma_s[i_start]
    if sma_end is not None and sma_start is not None:
        sma_part = 10.0 if sma_end > sma_start else (-10.0 if sma_end < sma_start else 0.0)
    else:
        sma_part = 0.0

    return _clamp(disp + er_part + swing_part + sma_part, -100.0, 100.0)


def _structure_confidence(
    prior: bool, climax: bool, ar: bool, st: bool, c_done: bool,
    strength: bool, lps: bool, accept: bool,
) -> int:
    """`f_structureConfidence`（原版第 616 行）：0–100 的结构置信度。"""
    return (
        (10 if prior else 0) + (15 if climax else 0) + (10 if ar else 0)
        + (15 if st else 0) + (15 if c_done else 0) + (15 if strength else 0)
        + (10 if lps else 0) + (10 if accept else 0)
    )


def _validation_score(
    trend: float, absorbed: bool, range_atr: float, st: int, opp: int, trav: int,
    clean: bool, cause: float, terminal: bool, strength: bool, lps: bool, accept: bool,
) -> int:
    """`f_validationScore`（原版第 619 行）：0–100 的验证评分。"""
    v = 0
    at = abs(trend)
    v += 10 if at >= priorTrendStrong else (5 if at >= priorTrendNeutral else 0)
    v += 15 if absorbed else 0
    if range_atr >= phaseBRangeMinATR and range_atr <= phaseBRangeMaxATR:
        v += 10
    elif range_atr >= phaseBRangeMinATR * 0.75 and range_atr <= phaseBRangeMaxATR * 1.5:
        v += 5
    v += 15 if st >= minPhaseBTests else (8 if st >= 1 else 0)
    v += 15 if (opp >= 1 and trav >= 2) else (8 if (opp >= 1 or trav >= 1) else 0)
    v += 10 if clean else 0
    v += 10 if cause >= 3.0 else (5 if cause >= 1.5 else 0)
    v += 10 if terminal else 0
    v += 3 if strength else 0
    v += 2 if lps else 0
    return min(v, 100)


def _cause_units(trav: int, b_age: int, p: int, range_atr: float) -> float:
    """`f_causeUnits`（原版第 636 行）。"""
    time_units = float(b_age) / max(float(p) * 6.0, 1.0)
    width_bonus = 0.5 if range_atr >= phaseBRangeMinATR else 0.0
    return time_units + float(trav) * 0.5 + width_bonus


def _test_class(
    near: bool, holds: bool, close_ok: bool,
    e: Optional[float], sp: Optional[float],
    c_e: Optional[float], c_s: Optional[float],
) -> int:
    """`f_testClass`（原版第 641 行）。"""
    if not near:
        return TEST_NONE
    if not holds:
        return TEST_FAILED
    if close_ok and _le(e, None if c_e is None else c_e * stMaxVolRatio) and \
            _le(sp, None if c_s is None else c_s * stMaxSpreadRatio):
        return TEST_GOOD
    return TEST_POOR


def _phase_a_min_bars(p: int) -> int:
    """`f_phaseAMinBars`（原版第 652 行）。"""
    return max(phaseAMinBars, max(int(p), 1) * 2)


def _phase_b_min_bars(p: int, range_atr: float) -> int:
    """`f_phaseBMinBars`（原版第 655 行）。"""
    p = max(int(p), 1)
    raw = int(round(max(float(p * 6), float(range_atr) * float(p) * 1.00)))
    return max(minPhaseBBars, min(raw, minPhaseBBars * 2))


def _phase_c_to_d_min_bars(p: int) -> int:
    """`f_phaseCToDMinBars`（原版第 661 行）。"""
    return max(phaseCToDMinBars, int(max(int(p), 1) / 2))


def _phase_d_min_bars(p: int) -> int:
    """`f_phaseDMinBars`（原版第 664 行）。"""
    return max(phaseDMinBars, max(int(p), 1))


def _excursion_recovery_limit(p: int) -> int:
    """`f_excursionRecoveryLimit`（原版第 667 行）。"""
    return max(excursionRecoveryBars, int(round(max(int(p), 1) * 0.75)))


def _phase_idle_limit(phase: int, has_ar: bool, p: int) -> Optional[int]:
    """`f_phaseIdleLimit`（原版第 670 行）：na 表示该阶段无闲置上限。"""
    p = max(int(p), 1)
    if phase == PHASE_A:
        return max(phaseAAfterArMaxBars, p * 6) if has_ar else max(maxARBars, p * 4)
    if phase == PHASE_B:
        return max(phaseBIdleMaxBars, p * 20)
    if phase == PHASE_C:
        return max(phaseCIdleMaxBars, p * 8)
    if phase == PHASE_D:
        return max(phaseCIdleMaxBars * 2, p * 16)
    return None


def _phase_b_character_improving(
    n: int, eff_sum: float, spr_sum: float,
    last_e: Optional[float], last_s: Optional[float],
) -> bool:
    """`f_phaseBCharacterImproving`（原版第 683 行）：努力/价差是否在收敛。"""
    if n < 2 or last_e is None or last_s is None:
        return False
    avg_e = eff_sum / n
    avg_s = spr_sum / n
    return last_e <= avg_e and last_s <= avg_s * 1.05


def _robust_edge(
    e1: Optional[float], e2: Optional[float], e3: Optional[float],
    orig: float, is_low: bool,
) -> float:
    """`f_robustEdge`（原版第 691 行）：三个枢轴的中位数，抗单点离群。

    不足三个枢轴时退化为「中位数与 `orig` 的均值」，再不足则直接用 `orig`。
    最后按 `is_low` 与 `orig` 取更保守的一侧（低点取 max、高点取 min）。
    """
    med: Optional[float] = None
    if e1 is not None and e2 is not None and e3 is not None:
        med = e1 + e2 + e3 - max(e1, e2, e3) - min(e1, e2, e3)
    elif e1 is not None and e2 is not None:
        med = ((e1 + e2) / 2.0 + orig) / 2.0
    elif e1 is not None:
        med = (e1 + orig) / 2.0
    if med is None:
        return orig
    return max(orig, med) if is_low else min(orig, med)


def _type_from(stop: int, out: int, bull: bool, bear: bool) -> int:
    """`f_typeFrom`（原版第 701 行）。"""
    if out == DIR_ACCUM:
        return TYPE_REACCUM if (stop == DIR_DIST or bull) else TYPE_ACCUM
    if out == DIR_DIST:
        return TYPE_REDIST if (stop == DIR_ACCUM or bear) else TYPE_DIST
    return TYPE_NONE


# ---------------------------------------------------------------------------
# 状态类型（字段名与原版 `type WS` / `type PivotTriplet` 逐字一致）
# ---------------------------------------------------------------------------

@dataclass
class _PivotTriplet:
    """`type PivotTriplet`（原版第 355 行）：前三根同侧/对侧边缘枢轴。"""

    b1: Optional[int] = None
    t1: Optional[float] = None
    p1: Optional[float] = None
    b2: Optional[int] = None
    t2: Optional[float] = None
    p2: Optional[float] = None
    b3: Optional[int] = None
    t3: Optional[float] = None
    p3: Optional[float] = None


@dataclass
class _WS:
    """`type WS`（原版第 367–484 行）。

    字段名与原版逐字一致，`None` 等价于 Pine 的 `na`。
    """

    stopSide: int = DIR_NONE
    outcome: int = DIR_NONE
    phase: int = PHASE_NONE
    ev: int = EV_NONE
    startBar: Optional[int] = None
    pivLen: Optional[int] = None
    birthTrend: Optional[float] = None
    ctxBull: bool = False
    ctxBear: bool = False
    climaxBar: Optional[int] = None
    climaxTime: Optional[float] = None
    climaxPrice: Optional[float] = None
    climaxEff: Optional[float] = None
    climaxSpr: Optional[float] = None
    climaxATR: Optional[float] = None
    climaxScore: int = 0
    absorbed: bool = False
    psTime: Optional[float] = None
    psPrice: Optional[float] = None
    psScore: int = 0
    origHigh: Optional[float] = None
    origLow: Optional[float] = None
    rangeHigh: Optional[float] = None
    rangeLow: Optional[float] = None
    arRunPrice: Optional[float] = None
    arRunBar: Optional[int] = None
    arRunTime: Optional[float] = None
    arOK: bool = False
    arBar: Optional[int] = None
    arTime: Optional[float] = None
    arPrice: Optional[float] = None
    stCount: int = 0
    stN: int = 0
    stEffSum: float = 0.0
    stSprSum: float = 0.0
    lastGoodEff: Optional[float] = None
    lastGoodSpr: Optional[float] = None
    stBar: Optional[int] = None
    stTime: Optional[float] = None
    stPrice: Optional[float] = None
    stHist: _PivotTriplet = None  # type: ignore[assignment]  # __post_init__ 补齐
    stScore: int = 0
    edge1: Optional[float] = None
    edge2: Optional[float] = None
    edge3: Optional[float] = None
    oppCount: int = 0
    opN: int = 0
    opEffSum: float = 0.0
    opSprSum: float = 0.0
    opLastBar: Optional[int] = None
    opLastPrice: Optional[float] = None
    opHist: _PivotTriplet = None  # type: ignore[assignment]  # __post_init__ 补齐
    travCount: int = 0
    lastZone: int = 0
    lastZoneBar: Optional[int] = None
    lastBadBar: Optional[int] = None
    bStartBar: Optional[int] = None
    bStartTime: Optional[float] = None
    cStartTime: Optional[float] = None
    cReadyBar: Optional[int] = None
    dStartBar: Optional[int] = None
    dStartTime: Optional[float] = None
    eStartBar: Optional[int] = None
    eStartTime: Optional[float] = None
    pend: bool = False
    pendEdge: int = DIR_NONE
    pendStartBar: Optional[int] = None
    pendExtBar: Optional[int] = None
    pendExt: Optional[float] = None
    pendExtTime: Optional[float] = None
    pendEff: Optional[float] = None
    pendSpr: Optional[float] = None
    pendCoolBar: Optional[int] = None
    provEdge: int = DIR_NONE
    provBar: Optional[int] = None
    provPrice: Optional[float] = None
    provTime: Optional[float] = None
    provEff: Optional[float] = None
    provSpr: Optional[float] = None
    provScore: int = 0
    exc: bool = False
    excTested: bool = False
    excBar: Optional[int] = None
    excPrice: Optional[float] = None
    excTime: Optional[float] = None
    excEff: Optional[float] = None
    excSpr: Optional[float] = None
    excScore: int = 0
    testTime: Optional[float] = None
    testPrice: Optional[float] = None
    testScore: int = 0
    strBar: Optional[int] = None
    strTime: Optional[float] = None
    strPrice: Optional[float] = None
    strScore: int = 0
    lpsTime: Optional[float] = None
    lpsPrice: Optional[float] = None
    lpsBar: Optional[int] = None
    lpsScore: int = 0
    entryTime: Optional[float] = None
    entryPrice: Optional[float] = None
    entryKind: int = ENTRY_NONE
    outCount: int = 0
    lastEventBar: Optional[int] = None
    lastEventTime: Optional[float] = None
    lastEventPrice: Optional[float] = None
    lastActBar: Optional[int] = None
    cfPrior: bool = False
    cfClimax: bool = False
    cfAR: bool = False
    cfST: bool = False
    cfExc: bool = False
    cfTest: bool = False
    cfStrength: bool = False
    cfLPS: bool = False
    cfAccept: bool = False

    def __post_init__(self) -> None:
        # 原版 `var WS s = WS.new()` 后紧跟 `f_initPivotHistory(s)`
        # （第 1092–1093 行）；每次 `s := WS.new()` 也配一次。
        self.stHist = _PivotTriplet()
        self.opHist = _PivotTriplet()


# ---------------------------------------------------------------------------
# 战役级辅助函数
# ---------------------------------------------------------------------------

def _set_event(s: _WS, ev: int, i: int, t: Optional[float], close_i: float) -> None:
    """`f_setEvent`（原版第 706 行）。"""
    s.ev = ev
    s.lastEventBar = i
    s.lastEventTime = t
    s.lastEventPrice = close_i
    s.lastActBar = i


def _bias_dir(s: _WS) -> int:
    """`f_biasDir`（原版第 714 行）：尚未定局时由上下文偏向来定方向。"""
    if s.stopSide == DIR_ACCUM:
        return DIR_DIST if s.ctxBear else DIR_ACCUM
    if s.stopSide == DIR_DIST:
        return DIR_ACCUM if s.ctxBull else DIR_DIST
    return DIR_NONE


def _type_ws(s: _WS) -> int:
    """`f_typeWS`（原版第 717 行）。"""
    o = s.outcome if s.outcome != DIR_NONE else _bias_dir(s)
    if s.stopSide == DIR_NONE:
        return TYPE_NONE
    return _type_from(s.stopSide, o, s.ctxBull, s.ctxBear)


def _range_atr_ws(s: _WS, atr_now: float, mintick: float) -> float:
    """`f_rangeATRWS`（原版第 721 行）：区间高度 / 高潮时 ATR。"""
    if s.rangeHigh is None or s.rangeLow is None:
        return 0.0
    return abs(s.rangeHigh - s.rangeLow) / max(_nz(s.climaxATR, atr_now), mintick)


def _hard_level(s: _WS, side: int) -> Optional[float]:
    """`f_hardLevel`（原版第 724 行）：该侧不可失守的硬位。"""
    if side == DIR_ACCUM:
        if s.exc and s.outcome == DIR_ACCUM and s.excPrice is not None:
            return s.excPrice
        if s.origLow is None:
            return s.rangeLow
        return min(s.origLow, _nz(s.rangeLow, s.origLow))
    if s.exc and s.outcome == DIR_DIST and s.excPrice is not None:
        return s.excPrice
    if s.origHigh is None:
        return s.rangeHigh
    return max(s.origHigh, _nz(s.rangeHigh, s.origHigh))


def _seed(
    s: _WS, side: int, score: int, p_len: Optional[int], trend: float,
    bull: bool, bear: bool, atr_now: float, eff_now: float, spr_now: float,
    i: int, t: Optional[float], low_i: float, high_i: float,
) -> None:
    """`f_seed`（原版第 732 行）：SC/BC 确认 → 起一档新战役（Phase A）。"""
    s.stopSide = side
    s.phase = PHASE_A
    s.startBar = i
    s.pivLen = p_len
    s.birthTrend = trend
    s.ctxBull = bull
    s.ctxBear = bear
    s.climaxBar = i
    s.climaxTime = t
    s.climaxPrice = low_i if side == DIR_ACCUM else high_i
    s.climaxEff = eff_now
    s.climaxSpr = spr_now
    s.climaxATR = atr_now
    s.climaxScore = score
    if side == DIR_ACCUM:
        s.origLow = low_i
        s.rangeLow = low_i
    else:
        s.origHigh = high_i
        s.rangeHigh = high_i
    s.cfPrior = True
    _set_event(s, EV_SC if side == DIR_ACCUM else EV_BC, i, t, (low_i if side == DIR_ACCUM else high_i))


def _freeze_ar(s: _WS) -> None:
    """`f_freezeAR`（原版第 756 行）：把 AR 的运行极值固化成对侧边界。"""
    s.arBar = s.arRunBar
    s.arTime = s.arRunTime
    s.arPrice = s.arRunPrice
    s.cfAR = True
    if s.stopSide == DIR_ACCUM:
        s.origHigh = s.arRunPrice
        s.rangeHigh = s.arRunPrice
    else:
        s.origLow = s.arRunPrice
        s.rangeLow = s.arRunPrice


def _register_st(
    s: _WS, price: float, t: Optional[float], pb: int,
    e: Optional[float], sp: Optional[float], score: int,
) -> None:
    """`f_registerST`（原版第 776 行）：登记一次二次测试并重算稳健边界。"""
    s.stCount += 1
    s.stN += 1
    s.stEffSum += _nz(e)
    s.stSprSum += _nz(sp)
    s.lastGoodEff = e
    s.lastGoodSpr = sp
    s.stBar = pb
    s.stTime = t
    s.stPrice = price
    h = s.stHist
    if h.t1 is None:
        h.b1, h.t1, h.p1 = pb, t, price
    elif h.t2 is None and pb > _nz(h.b1, -1):
        h.b2, h.t2, h.p2 = pb, t, price
    elif h.t3 is None and pb > _nz(h.b2, _nz(h.b1, -1)):
        h.b3, h.t3, h.p3 = pb, t, price
    s.stScore = score
    s.edge3 = s.edge2
    s.edge2 = s.edge1
    s.edge1 = price
    if s.stopSide == DIR_ACCUM:
        s.rangeLow = _robust_edge(s.edge1, s.edge2, s.edge3, _nz(s.origLow), True)
    else:
        s.rangeHigh = _robust_edge(s.edge1, s.edge2, s.edge3, _nz(s.origHigh), False)
    s.cfST = True
    _set_event(s, EV_ST, pb, t, price)


def _register_opp(
    s: _WS, price: float, t: Optional[float], pb: int,
    e: Optional[float], sp: Optional[float], i: int,
) -> None:
    """`f_registerOpp`（原版第 810 行）：登记一次对侧边缘测试。"""
    s.oppCount += 1
    s.opN += 1
    s.opEffSum += _nz(e)
    s.opSprSum += _nz(sp)
    s.opLastBar = pb
    s.opLastPrice = price
    h = s.opHist
    if h.t1 is None:
        h.b1, h.t1, h.p1 = pb, t, price
    elif h.t2 is None and pb > _nz(h.b1, -1):
        h.b2, h.t2, h.p2 = pb, t, price
    elif h.t3 is None and pb > _nz(h.b2, _nz(h.b1, -1)):
        h.b3, h.t3, h.p3 = pb, t, price
    s.lastActBar = i


def _clear_terminal(s: _WS) -> None:
    """`f_clearTerminal`（原版第 833 行）：清掉 Phase C 的探针/测试痕迹。"""
    s.exc = False
    s.excTested = False
    s.excBar = None
    s.excPrice = None
    s.excTime = None
    s.excEff = None
    s.excSpr = None
    s.excScore = 0
    s.testTime = None
    s.testPrice = None
    s.testScore = 0
    s.cfExc = False
    s.cfTest = False
    s.cReadyBar = None
    s.cStartTime = None


def _exc_from_pend(s: _WS, score: int, i: int, t: Optional[float], close_i: float) -> None:
    """`f_excFromPend`（原版第 851 行）：探测确认 → Spring/UTAD 成立。"""
    s.exc = True
    s.excTested = False
    s.excBar = s.pendExtBar
    s.excPrice = s.pendExt
    s.excTime = s.pendExtTime
    s.excEff = s.pendEff
    s.excSpr = s.pendSpr
    s.excScore = score
    s.cfExc = True
    s.outcome = s.pendEdge
    s.cStartTime = s.pendExtTime
    s.phase = PHASE_C
    s.cReadyBar = None
    s.pend = False
    s.provEdge = DIR_NONE
    _set_event(s, EV_SPRING if s.outcome == DIR_ACCUM else EV_UTAD, i, t, close_i)


def _adopt_prov(s: _WS) -> None:
    """`f_adoptProv`（原版第 869 行）：把暂存的探针转正为 Spring/UTAD。"""
    s.exc = True
    s.excTested = False
    s.excBar = s.provBar
    s.excPrice = s.provPrice
    s.excTime = s.provTime
    s.excEff = s.provEff
    s.excSpr = s.provSpr
    s.excScore = s.provScore
    s.cfExc = True
    s.outcome = s.provEdge
    s.cStartTime = s.provTime
    s.provEdge = DIR_NONE


def _enter_ctest(
    s: _WS, outc: int, t: Optional[float], price: float, score: int,
    i: int, close_i: float,
) -> None:
    """`f_enterCTest`（原版第 884 行）：无穿透的终端测试路径进入 Phase C。"""
    s.phase = PHASE_C
    s.outcome = outc
    s.cStartTime = t
    s.cReadyBar = i
    s.testTime = t
    s.testPrice = price
    s.testScore = score
    s.cfTest = True
    s.pend = False
    _set_event(s, EV_CTEST_ACC if outc == DIR_ACCUM else EV_CTEST_DST, i, close_i, close_i)


def _promote_d(
    s: _WS, outc: int, score: int, price: float,
    i: int, t: Optional[float], close_i: float,
) -> None:
    """`f_promoteD`（原版第 896 行）：SOS/SOW 成立 → Phase D。"""
    s.outcome = outc
    s.phase = PHASE_D
    s.dStartBar = i
    s.dStartTime = t
    s.strBar = i
    s.strTime = t
    s.strPrice = price
    s.strScore = score
    s.cfStrength = True
    s.pend = False
    s.outCount = 0
    _set_event(s, EV_SOS if outc == DIR_ACCUM else EV_SOW, i, close_i, close_i)


def _demote_to_b(s: _WS, i: int) -> None:
    """`f_demoteToB`（原版第 910 行）：Phase D 跌破中位 → 退回 Phase B。"""
    _clear_terminal(s)
    s.outcome = DIR_NONE
    s.phase = PHASE_B
    s.strBar = None
    s.strTime = None
    s.strPrice = None
    s.strScore = 0
    s.lpsTime = None
    s.lpsPrice = None
    s.lpsBar = None
    s.lpsScore = 0
    s.dStartBar = None
    s.dStartTime = None
    s.cfStrength = False
    s.cfLPS = False
    s.provEdge = DIR_NONE
    s.pend = False
    s.outCount = 0
    s.lastBadBar = i
    s.lastActBar = i


def _conf_ws(s: _WS) -> int:
    """`f_confWS`（原版第 933 行）。"""
    c_done = s.cfTest or (s.cfExc and s.cfStrength)
    return _structure_confidence(
        s.cfPrior, s.cfClimax, s.cfAR, s.cfST, c_done, s.cfStrength, s.cfLPS, s.cfAccept
    )


def _validation_ws(
    s: _WS, p: int, trend_now: Optional[float], atr_now: float,
    i: int, mintick: float,
) -> int:
    """`f_validationWS`（原版第 937 行）。"""
    r_atr = _range_atr_ws(s, atr_now, mintick)
    b_age = max(i - s.bStartBar, 0) if s.bStartBar is not None else 0
    bad_age = -1 if s.lastBadBar is None else i - s.lastBadBar
    clean = bad_age < 0 or bad_age >= int(round(p * phaseBBadTestCooldownFactor))
    cause = _cause_units(s.travCount, b_age, p, r_atr)
    terminal = (s.cfTest and (not s.exc or s.excTested)) or (s.cfExc and s.cfStrength)
    if s.stopSide == DIR_NONE:
        return 0
    return _validation_score(
        _nz(s.birthTrend, _nz(trend_now)), s.absorbed, r_atr, s.stCount, s.oppCount,
        s.travCount, clean, cause, terminal, s.cfStrength, s.cfLPS, s.cfAccept,
    )


def _mature_ws(s: _WS, p: int, atr_now: float, i: int, mintick: float) -> bool:
    """`f_matureWS`（原版第 946 行）：Phase B 是否已「成因充分」。"""
    r_atr = _range_atr_ws(s, atr_now, mintick)
    b_age = (i - s.bStartBar) if s.bStartBar is not None else 0
    b_min = _phase_b_min_bars(p, r_atr)
    bad_age = -1 if s.lastBadBar is None else i - s.lastBadBar
    clean = bad_age < 0 or bad_age >= int(round(p * phaseBBadTestCooldownFactor))
    opp = minPhaseBOppositeTests <= 0 or s.oppCount >= minPhaseBOppositeTests
    trav = minPhaseBTraversals <= 0 or s.travCount >= minPhaseBTraversals
    ch = _phase_b_character_improving(s.stN, s.stEffSum, s.stSprSum, s.lastGoodEff, s.lastGoodSpr)
    support = (1 if opp else 0) + (1 if trav else 0) + (1 if clean else 0) + (1 if ch else 0)
    sane = r_atr >= phaseBRangeMinATR * 0.75 and r_atr <= phaseBRangeMaxATR * 2.0
    return bool(
        s.phase == PHASE_B and b_age >= b_min and s.stCount >= minPhaseBTests
        and sane and support >= 2
    )


def _entry_readiness_ws(
    s: _WS, min_conf: int, val_floor: int, atr_now: float,
    i: int, close_i: float, t: Optional[float], tf_secs: float, mintick: float,
) -> int:
    """`f_entryReadinessWS`（原版第 959 行）：9 项入场条件打点（0–9）。"""
    bull = s.outcome == DIR_ACCUM
    if s.rangeHigh is None or s.rangeLow is None:
        dist_atr = 99.0
    elif bull:
        dist_atr = (close_i - s.rangeHigh) / max(atr_now, mintick)
    else:
        dist_atr = (s.rangeLow - close_i) / max(atr_now, mintick)
    if s.lastEventTime is not None and t is not None:
        # Pine: (time - lastEventTime) / (in_seconds(period) * 1000)，两边都是毫秒。
        # 这里 times 是 epoch 秒，故除数就是 tf_secs（少一层 *1000）。
        age_bars = (float(t) - float(s.lastEventTime)) / max(float(tf_secs), 1.0)
    else:
        age_bars = 999.0
    terminal = (s.exc and s.excTested) or (not s.exc and s.cfTest)
    conf = _conf_ws(s)
    p = _nz(s.pivLen, pivotLen)
    r = 0
    r += 1 if s.outcome != DIR_NONE else 0
    r += 1 if s.phase >= PHASE_C else 0
    r += 1 if terminal else 0
    r += 1 if s.cfStrength else 0
    r += 1 if s.cfLPS else 0
    r += 1 if conf >= min_conf else 0
    r += 1 if _validation_ws(s, p, s.birthTrend, atr_now, i, mintick) >= val_floor else 0
    r += 1 if dist_atr <= 2.0 else 0
    r += 1 if age_bars <= 50.0 else 0
    return r


def _describe(
    s: _WS, p: int, atr_now: float, rs_counts: list[int], last_why: int,
    regime: int, i: int, mintick: float,
) -> tuple[str, list[dict[str, Any]]]:
    """`f_describe`（原版第 981 行）。

    返回 `(next, checks)`：

    - `next`：原版 `nx` 的**逐字原文**——它说清「现在卡在哪个条件上」，
      例如 `Building cause: tests 1/2  age 18/30  support 1/4 (need 2)`。
      这是给 LLM 最有用的一段。
    - `checks`：原版诊断表（`rows`）的结构化形式，每行
      `{name, current, required, ok}`。原版是画给人看的表格，这里改成数据。
    """
    nx = "Searching for SC / BC"
    rows: list[dict[str, Any]] = []
    camp_age = (i - s.startBar) if s.startBar is not None else 0
    r_atr = _range_atr_ws(s, atr_now, mintick)
    side_txt = "SC" if s.stopSide == DIR_ACCUM else "BC"

    def row(name: str, cur: Any, req: Any, ok: bool) -> None:
        rows.append({"name": name, "current": cur, "required": req, "ok": bool(ok)})

    if s.phase > PHASE_NONE:
        row("Campaign age", f"{camp_age} bars", "-", True)
        row("Swing width", f"{p} bars", "pre-climax", True)
        row(
            "Range height", f"{r_atr:.2f} ATR",
            f"{phaseBRangeMinATR:.1f}-{phaseBRangeMaxATR:.1f}",
            phaseBRangeMinATR <= r_atr <= phaseBRangeMaxATR,
        )

    if s.phase == PHASE_A:
        ar_move = 0.0
        if s.arRunPrice is not None and s.climaxPrice is not None:
            ar_move = abs(s.arRunPrice - s.climaxPrice) / max(atr_now, mintick)
        a_min = _phase_a_min_bars(p)
        if not s.absorbed:
            nx = f"Confirming stopping action after the {side_txt}"
        elif not s.arOK:
            nx = f"Waiting for AR ({minARATR:.1f} ATR counter-move)"
        else:
            nx = "Waiting for confirming ST (AR provisional)"
        row("Stopping action", "Confirmed" if s.absorbed else "Pending", "Confirmed", s.absorbed)
        row("AR displacement", f"{ar_move:.2f} ATR", f">= {minARATR:.2f}", s.arOK)
        row("Phase A age", str(camp_age), f">= {a_min}", camp_age >= a_min)

    elif s.phase == PHASE_B:
        b_age = (i - s.bStartBar) if s.bStartBar is not None else 0
        b_min = _phase_b_min_bars(p, r_atr)
        cool = int(round(p * phaseBBadTestCooldownFactor))
        bad_age = -1 if s.lastBadBar is None else i - s.lastBadBar
        opp_ok = minPhaseBOppositeTests <= 0 or s.oppCount >= minPhaseBOppositeTests
        trav_ok = minPhaseBTraversals <= 0 or s.travCount >= minPhaseBTraversals
        clean_ok = bad_age < 0 or bad_age >= cool
        ch_ok = _phase_b_character_improving(
            s.stN, s.stEffSum, s.stSprSum, s.lastGoodEff, s.lastGoodSpr
        )
        sup = (1 if opp_ok else 0) + (1 if trav_ok else 0) + (1 if clean_ok else 0) + (1 if ch_ok else 0)
        sane = r_atr >= phaseBRangeMinATR * 0.75 and r_atr <= phaseBRangeMaxATR * 2.0
        tests_ok = s.stCount >= minPhaseBTests
        age_ok = b_age >= b_min
        blockers = ""
        if not tests_ok:
            blockers += f"tests {s.stCount}/{minPhaseBTests}  "
        if not age_ok:
            blockers += f"age {b_age}/{b_min}  "
        if sup < 2:
            blockers += f"support {sup}/4 (need 2)  "
        if not sane:
            blockers += "range height out of band  "
        prov_txt = ""
        if s.provEdge == DIR_ACCUM:
            prov_txt = " | provisional Spring on file"
        elif s.provEdge == DIR_DIST:
            prov_txt = " | provisional UTAD on file"
        head = (
            "Cause built: watch for Spring/UTAD or a terminal test at either edge"
            if blockers == "" else "Building cause: " + blockers
        )
        nx = head + prov_txt
        row("Climax-side tests", str(s.stCount), f">= {minPhaseBTests}", tests_ok)
        row("Phase B age", str(b_age), f">= {b_min}", age_ok)
        row("Opposite-edge tests", str(s.oppCount), f">= {minPhaseBOppositeTests}", opp_ok)
        row("Traversals", str(s.travCount), f">= {minPhaseBTraversals}", trav_ok)
        row("Bad-test cooldown", "none" if bad_age < 0 else f"{bad_age} bars", f">= {cool}", clean_ok)
        row("Effort drying up", "Yes" if ch_ok else "No", "support clue", ch_ok)
        row("Support clues", f"{sup}/4", ">= 2", sup >= 2)

    elif s.phase == PHASE_C:
        formal = (not s.exc) or s.excTested
        want = "SOS" if s.outcome == DIR_ACCUM else "SOW"
        nx = f"Waiting for {want}" if formal else f"Waiting for Test, or a decisive {want}"
        opp_cyc = (regime == REGIME_MARKDOWN and s.outcome == DIR_ACCUM) or (
            regime == REGIME_MARKUP and s.outcome == DIR_DIST
        )
        if opp_cyc and camp_age < oppositeCycleMinBars:
            nx = f"Cycle reversal: building cause ({camp_age}/{oppositeCycleMinBars})"
        anchor = s.cReadyBar if s.cReadyBar is not None else s.excBar
        c_age = (i - anchor) if anchor is not None else 0
        c_min = _phase_c_to_d_min_bars(p)
        c_val = _validation_ws(s, p, s.birthTrend, atr_now, i, mintick)
        row("Route", "Spring" if s.outcome == DIR_ACCUM else "UTAD" if s.exc else "Terminal test", "-", True)
        row("Post-probe test", "n/a" if not s.exc else "Done" if s.excTested else "Pending",
            "preferred", (not s.exc) or s.excTested)
        row("Phase C development", str(c_age), f">= {c_min}", c_age >= c_min)
        row("Validation", str(c_val), f">= {phaseDValidationMin}", c_val >= phaseDValidationMin)

    elif s.phase == PHASE_D:
        lp = "LPS" if s.outcome == DIR_ACCUM else "LPSY"
        d_conf = _conf_ws(s)
        nx = "Waiting for acceptance beyond the range" if s.lpsTime is not None else f"Waiting for {lp}"
        row("SOS" if s.outcome == DIR_ACCUM else "SOW", "Confirmed", "-", True)
        row(lp, "Pending" if s.lpsTime is None else "Confirmed", "for Phase E", s.lpsTime is not None)
        row("Closes outside range", str(s.outCount), f">= {confirmBars}", s.outCount >= confirmBars)
        row("Maturity", str(d_conf), f">= {minConfidencePhaseD}", d_conf >= minConfidencePhaseD)

    elif s.phase == PHASE_E:
        nx = (
            "Markup underway: range should act as support"
            if s.outcome == DIR_ACCUM else
            "Markdown underway: range should act as resistance"
        )

    rs_txt = "/".join(str(rs_counts[k]) for k in range(7))
    why_txt = {
        RS_INVALID: "Invalidated", RS_ABSORB: "No absorption", RS_NORANGE: "No range formed",
        RS_DEPART: "Left range unconfirmed", RS_STALE: "Stale", RS_DEMOTE: "Demoted",
        RS_EEND: "Phase E ended",
    }.get(last_why, "-")
    row("Resets inv/abs/rng/dep/stale/dem/E", rs_txt, "-", True)
    row("Last reset", why_txt, "-", True)
    return nx, rows


def _off(base: Optional[float], k: float, atr: Optional[float]) -> Optional[float]:
    """Pine 的 `base + k * atr`：任一侧为 na 时结果为 na。"""
    if base is None or atr is None:
        return None
    return base + k * atr


def _pivot_at(
    highs: list[float], lows: list[float], i: int, p_len: int, *, high: bool,
) -> Optional[float]:
    """原版 `ta.pivothigh/low(high|low, pLen, pLen)` 在 bar `i` 上的取值。

    left = right = pLen；枢轴 bar = `i - pLen`；须**严格**高于/低于窗口
    `[i - 2*pLen, i]` 内其余所有 bar。窗口不足或不是枢轴时返回 None（Pine 的 na）。

    ⚠️ **等值口径（待用 TV 截图核对）**：这里用严格比较——窗口内出现**等值**
    的 bar 就不算枢轴。这与本仓库既有的 `smc_events.pivothigh/pivotlow` 一致。
    实测影响：15m 数据上等值会额外多出约 10% 的枢轴，1m 上超过 200%
    （低周期 tick 粒度相对价格波动太细，等值更常见），1h/4h 几乎为 0。
    """
    p = int(p_len)
    if p < 1:
        return None
    j = i - p
    if j - p < 0:
        return None
    src = highs if high else lows
    v = src[j]
    for k in range(j - p, i + 1):
        if k == j:
            continue
        if high:
            if src[k] >= v:
                return None
        elif src[k] <= v:
            return None
    return v


def _pivots_for(
    highs: list[float], lows: list[float], i: int, p_len: int,
) -> tuple[Optional[float], Optional[float]]:
    return (
        _pivot_at(highs, lows, i, p_len, high=True),
        _pivot_at(highs, lows, i, p_len, high=False),
    )


# ---------------------------------------------------------------------------
# 主引擎
# ---------------------------------------------------------------------------

def compute_wyckoff(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    *,
    times: Optional[list[float]] = None,
    strictness: str = "Standard",
    mintick: float = 1e-9,
) -> dict[str, Any]:
    """`f_engine`（原版第 1061–1765 行）—— 单遍扫描整个 OHLCV 序列。

    每根 bar 走一遍原版的 `if _commit` 分支（原版 `_commit = barstate.isconfirmed`，
    即只在收盘 bar 上改状态）。返回**最后一根 bar 的战役快照**，外加 `next`
    （「现在等什么」）与 `events`（本战役已确认的事件）。

    `times` 为 **epoch 秒**（本仓库 K 线的 `t` 单位；Gate REST 与本地库一致）。
    `strictness` ∈ Conservative / Standard / Aggressive，对应原版 `entryStrictness`。
    `mintick` 对应原版 `syminfo.mintick`（只用于下限保护，取不到时用极小值即可）。
    """
    n = len(closes)
    if n == 0:
        return {"error": "empty series"}
    if not (len(opens) == len(highs) == len(lows) == len(volumes) == n):
        return {"error": "OHLCV lengths differ"}
    if strictness not in STRICTNESS:
        strictness = "Standard"
    times = list(times) if times else [float(i) for i in range(n)]
    if len(times) != n:
        times = [float(i) for i in range(n)]
    mintick = float(mintick) if mintick and mintick > 0 else 1e-9

    # ------------------------------------------------------------------
    # Bar 级测量（原版第 1063–1161 行，全部是 `ta.*` 序列，可预计算）
    # ------------------------------------------------------------------
    rawVol = [_nz(v) for v in volumes]
    noVol: list[bool] = []
    cum = 0.0
    for v in rawVol:
        cum += v
        noVol.append(cum <= 0.0)

    spr = [max(highs[i] - lows[i], mintick) for i in range(n)]
    eff = [spr[i] if noVol[i] else rawVol[i] for i in range(n)]

    avgEffRaw = _pine_sma(eff, volLen)
    avgEff = [_nz(avgEffRaw[i], eff[i]) for i in range(n)]
    avgEffPrev = [
        _nz(avgEffRaw[i - 1], avgEff[i]) if i >= 1 else avgEff[i] for i in range(n)
    ]

    atrRaw = _pine_atr(highs, lows, closes, atrLen)
    a = [_nz(atrRaw[i], spr[i]) for i in range(n)]
    aPrev = [_nz(atrRaw[i - 1], a[i]) if i >= 1 else a[i] for i in range(n)]

    unit = [max(a[i], mintick) for i in range(n)]
    cPos = [(closes[i] - lows[i]) / spr[i] for i in range(n)]
    relE = [eff[i] / max(avgEff[i], 1e-10) for i in range(n)]
    sprATR = [spr[i] / unit[i] for i in range(n)]

    recentEff = _pine_sma(eff, climaxAbsorptionMinBars)
    recentLow = _pine_lowest(lows, climaxAbsorptionMinBars)
    recentHigh = _pine_highest(highs, climaxAbsorptionMinBars)

    lo30 = _pine_lowest(lows, extremeLen)
    hi30 = _pine_highest(highs, extremeLen)
    effHi30 = _pine_highest(eff, extremeLen)
    priorLo = [lo30[i - 1] if i >= 1 else None for i in range(n)]
    priorHi = [hi30[i - 1] if i >= 1 else None for i in range(n)]
    effHiPrior = [effHi30[i - 1] if i >= 1 else None for i in range(n)]
    newLow = [priorLo[i] is not None and lows[i] <= priorLo[i] for i in range(n)]
    newHigh = [priorHi[i] is not None and highs[i] >= priorHi[i] for i in range(n)]

    aBase = _pine_sma(a, adaptiveSwingVolLen)
    adaptLen: list[int] = []
    for i in range(n):
        base = aBase[i]
        ratio = (a[i] / base) if (base is not None and base > 0.0) else 1.0
        adaptLen.append(
            max(adaptiveSwingMin, min(adaptiveSwingMax, int(round(pivotLen * ratio))))
        )
    liveLen = adaptLen if swingModeInput == "ATR Adaptive" else [pivotLen] * n
    preLen = [liveLen[i - 3] if i >= 3 else liveLen[i] for i in range(n)]

    longSma = _pine_sma(closes, continuationTrendLen)
    trendBars = [
        max(trendLen, min(priorTrendMaxBars, int(round(liveLen[i] * priorTrendPivotFactor))))
        for i in range(n)
    ]
    trendEnd = [
        max(2, min(8, int(round(liveLen[i] * 0.75)))) for i in range(n)
    ]
    longBull: list[bool] = []
    longBear: list[bool] = []
    for i in range(n):
        c1 = closes[i - 1] if i >= 1 else 0.0
        s1 = _nz(longSma[i - 1] if i >= 1 else None)
        s20 = _nz(longSma[i - 20] if i >= 20 else None)
        longBull.append(c1 > s1 and s1 > s20)
        longBear.append(
            (longSma[i - 20] if i >= 20 else None) is not None and c1 < s1 and s1 < s20
        )

    effort2 = [max(relE[i], 0.01) * max(sprATR[i], 0.01) for i in range(n)]
    dnEffc = [
        max(_nz(closes[i - 1] if i >= 1 else None, closes[i]) - closes[i], 0.0)
        / unit[i] / effort2[i]
        for i in range(n)
    ]
    upEffc = [
        max(closes[i] - _nz(closes[i - 1] if i >= 1 else None, closes[i]), 0.0)
        / unit[i] / effort2[i]
        for i in range(n)
    ]
    dnBaseRaw = _pine_sma(dnEffc, prelimEfficiencyLookback)
    upBaseRaw = _pine_sma(upEffc, prelimEfficiencyLookback)
    dnBase = [dnBaseRaw[i - 1] if i >= 1 else None for i in range(n)]
    upBase = [upBaseRaw[i - 1] if i >= 1 else None for i in range(n)]
    dnAbsorb = [
        dnBase[i] is not None and dnEffc[i] <= dnBase[i] * prelimEfficiencyRatio
        for i in range(n)
    ]
    upAbsorb = [
        upBase[i] is not None and upEffc[i] <= upBase[i] * prelimEfficiencyRatio
        for i in range(n)
    ]
    nearRecentLow = [
        priorLo[i] is not None and lows[i] <= priorLo[i] + a[i] * prelimExtremeATR
        for i in range(n)
    ]
    nearRecentHigh = [
        priorHi[i] is not None and highs[i] >= priorHi[i] - a[i] * prelimExtremeATR
        for i in range(n)
    ]

    hiCInv = _pine_highest(closes, structureInvalidationBars)
    loCInv = _pine_lowest(closes, structureInvalidationBars)
    hiCDep = _pine_highest(closes, phaseBDepartureBars)
    loCDep = _pine_lowest(closes, phaseBDepartureBars)
    hiCA = _pine_highest(closes, phaseAPrematureDepartureBars)
    loCA = _pine_lowest(closes, phaseAPrematureDepartureBars)
    hiCConf = _pine_highest(closes, confirmBars)
    loCConf = _pine_lowest(closes, confirmBars)
    hiCDir = _pine_highest(closes, directAcceptanceBars)
    loCDir = _pine_lowest(closes, directAcceptanceBars)
    hiCFail = _pine_highest(closes, phaseDFailBars)
    loCFail = _pine_lowest(closes, phaseDFailBars)

    mbLow = _pine_lowest(lows, sosMultiBarLen)
    mbHigh = _pine_highest(highs, sosMultiBarLen)
    mbEffSum = _roll_sum(eff, sosMultiBarLen)
    mbEffRatio = [
        (mbEffSum[i] / max(avgEff[i] * sosMultiBarLen, 1e-10))
        if mbEffSum[i] is not None else None
        for i in range(n)
    ]
    mbSOS = [
        _ge((closes[i] - mbLow[i]) / unit[i] if mbLow[i] is not None else None,
            sosMultiBarATR)
        and _ge(mbEffRatio[i], sosMultiBarEffort) and cPos[i] >= 0.50
        for i in range(n)
    ]
    mbSOW = [
        _ge((mbHigh[i] - closes[i]) / unit[i] if mbHigh[i] is not None else None,
            sosMultiBarATR)
        and _ge(mbEffRatio[i], sosMultiBarEffort) and cPos[i] <= 0.50
        for i in range(n)
    ]
    res3Up = [
        max(closes[i] - _nz(closes[i - 3] if i >= 3 else None, closes[i]), 0.0) / unit[i]
        for i in range(n)
    ]
    res3Dn = [
        max(_nz(closes[i - 3] if i >= 3 else None, closes[i]) - closes[i], 0.0) / unit[i]
        for i in range(n)
    ]
    sosScore = [
        (1 if relE[i] >= strengthVolMult else 0)
        + (1 if sprATR[i] >= strengthSpreadATR else 0)
        + (1 if cPos[i] >= sosCloseMin else 0)
        + (1 if (closes[i] > opens[i] and closes[i] - opens[i] >= a[i] * 0.50) else 0)
        for i in range(n)
    ]
    sowScore = [
        (1 if relE[i] >= strengthVolMult else 0)
        + (1 if sprATR[i] >= strengthSpreadATR else 0)
        + (1 if cPos[i] <= sowCloseMax else 0)
        + (1 if (closes[i] < opens[i] and opens[i] - closes[i] >= a[i] * 0.50) else 0)
        for i in range(n)
    ]
    close1 = [closes[i - 1] if i >= 1 else closes[i] for i in range(n)]
    close2 = [closes[i - 2] if i >= 2 else closes[i] for i in range(n)]
    ctxBars = max(priorTrendMaxBars, continuationTrendLen) * 2

    # ------------------------------------------------------------------
    # 跨 bar 累积状态（原版第 1092–1104 行的 `var`）
    # ------------------------------------------------------------------
    s = _WS()
    regime = REGIME_NONE
    regimeBar: Optional[int] = None
    prelimBar: Optional[int] = None
    prelimTime: Optional[float] = None
    prelimPrice: Optional[float] = None
    prelimDir = DIR_NONE
    prelimScore = 0
    lastPLBar: Optional[int] = None
    lastPHBar: Optional[int] = None
    rs_counts = [0] * 7
    last_why = -1

    ph_series: list[Optional[float]] = [None] * n
    pl_series: list[Optional[float]] = [None] * n
    tf_secs = _tf_secs(times)

    for i in range(n):
        # ---- 顶部：pLen 用的是**进入本 bar 时**的状态（原版第 1106 行） ----
        if s.phase != PHASE_NONE and s.pivLen is not None:
            p_len = int(s.pivLen)
        else:
            p_len = int(liveLen[i])
        ph, pl = _pivots_for(highs, lows, i, p_len)
        ph_series[i] = ph
        pl_series[i] = pl
        pivBar = i - p_len

        pA = max(_nz(_at(atrRaw, i, p_len), a[i]), mintick)
        pEff = _nz(_at(eff, i, p_len), eff[i])
        pSpr = max(
            _nz(_at(highs, i, p_len), highs[i]) - _nz(_at(lows, i, p_len), lows[i]),
            mintick,
        )
        pCPos = (
            _nz(_at(closes, i, p_len), closes[i]) - _nz(_at(lows, i, p_len), lows[i])
        ) / pSpr
        pAvgEff = _nz(_at(avgEffRaw, i, p_len), avgEff[i])
        pTime = float(_nz(_at(times, i, p_len), times[i]))

        trendScore = _prior_trend_score(
            closes, a, ph_series, pl_series, longSma, i,
            trendBars[i], trendEnd[i], mintick,
        )
        downTrend = trendScore <= -priorTrendNeutral
        upTrend = trendScore >= priorTrendNeutral
        strongTrend = abs(trendScore) >= priorTrendStrong

        sig = 0

        # ============ if _commit（原版第 1163 行） ============
        p0 = _nz(s.pivLen, liveLen[i])
        rk0 = s.rangeHigh is not None and s.rangeLow is not None
        rH0 = s.rangeHigh
        rL0 = s.rangeLow
        rHt0 = max(rH0 - rL0, mintick) if rk0 else None
        rMid0 = (rH0 + rL0) / 2.0 if rk0 else None
        rATR0 = _range_atr_ws(s, a[i], mintick)
        probeOpen = bool(
            s.pend and s.pendStartBar is not None
            and (i - s.pendStartBar) <= _excursion_recovery_limit(p0)
        )
        bAge0 = (i - s.bStartBar) if s.bStartBar is not None else 0
        bMin0 = _phase_b_min_bars(p0, rATR0)
        structAge = (i - s.startBar) if s.startBar is not None else 0
        ageOKBull = regime != REGIME_MARKDOWN or structAge >= oppositeCycleMinBars
        ageOKBear = regime != REGIME_MARKUP or structAge >= oppositeCycleMinBars
        hardBull0 = _hard_level(s, DIR_ACCUM)
        hardBear0 = _hard_level(s, DIR_DIST)
        resetWhy = -1

        # ---- 淘汰不再成立的战役（原版第 1182–1235 行） ----
        if s.phase == PHASE_A:
            aClimaxAge = (i - s.climaxBar) if s.climaxBar is not None else 0
            aInvAcc = s.stopSide == DIR_ACCUM and _lt(
                _at(hiCInv, i), _off(s.climaxPrice, -structureInvalidationATR, a[i])
            )
            aInvDst = s.stopSide == DIR_DIST and _gt(
                _at(loCInv, i), _off(s.climaxPrice, structureInvalidationATR, a[i])
            )
            aAbsorbExp = (not s.absorbed) and aClimaxAge > climaxAbsorptionMaxBars
            aNoAR = (not s.arOK) and aClimaxAge > maxARBars
            aOverExt = bool(
                s.arOK and s.arRunPrice is not None
                and abs(s.arRunPrice - _nz(s.climaxPrice))
                / max(_nz(s.climaxATR, a[i]), mintick) > arMaxExtensionATR
            )
            aBuf = max(rHt0 * phaseAPrematureDepartureTRFrac, a[i] * phaseAPrematureDepartureATR) if rk0 else None
            aDepart = bool(
                aClimaxAge > maxARBars and rk0
                and (
                    (s.stopSide == DIR_ACCUM and _gt(_at(loCA, i), _off(rH0, 1.0, aBuf)))
                    or (s.stopSide == DIR_DIST and _lt(_at(hiCA, i), _off(rL0, -1.0, aBuf)))
                )
            )
            if aInvAcc or aInvDst:
                resetWhy = RS_INVALID
            elif aAbsorbExp:
                resetWhy = RS_ABSORB
            elif aNoAR or aOverExt or aDepart:
                resetWhy = RS_NORANGE

        if s.phase == PHASE_C and not probeOpen and resetWhy < 0:
            cBullDead = s.outcome == DIR_ACCUM and hardBull0 is not None and _lt(
                _at(hiCInv, i), _off(hardBull0, -structureInvalidationATR, a[i])
            )
            cBearDead = s.outcome == DIR_DIST and hardBear0 is not None and _gt(
                _at(loCInv, i), _off(hardBear0, structureInvalidationATR, a[i])
            )
            if cBullDead or cBearDead:
                resetWhy = RS_INVALID

        if s.phase == PHASE_D and rk0 and resetWhy < 0:
            if s.outcome == DIR_ACCUM:
                if hardBull0 is not None and _lt(
                    _at(hiCInv, i), _off(hardBull0, -structureInvalidationATR, a[i])
                ):
                    resetWhy = RS_INVALID
                elif _lt(_at(hiCFail, i), rMid0):
                    _demote_to_b(s, i)
                    rs_counts[RS_DEMOTE] += 1
            elif s.outcome == DIR_DIST:
                if hardBear0 is not None and _gt(
                    _at(loCInv, i), _off(hardBear0, structureInvalidationATR, a[i])
                ):
                    resetWhy = RS_INVALID
                elif _gt(_at(loCFail, i), rMid0):
                    _demote_to_b(s, i)
                    rs_counts[RS_DEMOTE] += 1

        if s.phase == PHASE_E and rk0 and resetWhy < 0:
            eFail = (
                _lt(_at(hiCFail, i), rMid0) if s.outcome == DIR_ACCUM
                else _gt(_at(loCFail, i), rMid0)
            )
            eOld = bool(
                phaseEMaxBars > 0 and s.eStartBar is not None
                and (i - s.eStartBar) > phaseEMaxBars
            )
            if eFail or eOld:
                resetWhy = RS_EEND

        idleLim = _phase_idle_limit(s.phase, s.arOK, p0)
        if (resetWhy < 0 and not probeOpen and idleLim is not None
                and s.lastActBar is not None and (i - s.lastActBar) > idleLim):
            resetWhy = RS_STALE

        if resetWhy >= 0:
            rs_counts[resetWhy] += 1
            last_why = resetWhy
            s = _WS()

        # ---- 记录初步支撑/供给（PS / PSY），并起新高潮（原版第 1237–1276 行） ----
        if s.phase == PHASE_NONE or s.phase == PHASE_E:
            psSc = (
                (1 if downTrend else 0) + (1 if nearRecentLow[i] else 0)
                + (1 if relE[i] >= prelimVolMult else 0)
                + (1 if sprATR[i] >= 0.80 else 0)
                + (1 if cPos[i] >= 0.45 else 0) + (1 if dnAbsorb[i] else 0)
            )
            psySc = (
                (1 if upTrend else 0) + (1 if nearRecentHigh[i] else 0)
                + (1 if relE[i] >= prelimVolMult else 0)
                + (1 if sprATR[i] >= 0.80 else 0)
                + (1 if cPos[i] <= 0.55 else 0) + (1 if upAbsorb[i] else 0)
            )
            psStale = prelimBar is None or (i - prelimBar) > extremeLen
            if (downTrend and nearRecentLow[i] and relE[i] >= prelimVolMult
                    and psSc >= prelimMinScore
                    and (prelimDir != DIR_ACCUM or psStale or psSc >= prelimScore)):
                prelimBar, prelimTime, prelimPrice = i, times[i], lows[i]
                prelimDir, prelimScore = DIR_ACCUM, psSc
            elif (upTrend and nearRecentHigh[i] and relE[i] >= prelimVolMult
                    and psySc >= prelimMinScore
                    and (prelimDir != DIR_DIST or psStale or psySc >= prelimScore)):
                prelimBar, prelimTime, prelimPrice = i, times[i], highs[i]
                prelimDir, prelimScore = DIR_DIST, psySc

        relaxK = relaxedClimaxFactor if strongTrend else 1.0
        volHit = eff[i] >= avgEffPrev[i] * climaxVolMult * relaxK
        sprHit = spr[i] >= aPrev[i] * climaxSpreadMult * relaxK
        exceptional = bool(
            (effHiPrior[i] is not None and eff[i] >= effHiPrior[i])
            or spr[i] >= aPrev[i] * climaxSpreadMult * 1.5
        )
        scSc = (
            (1 if downTrend else 0) + (1 if newLow[i] else 0) + (1 if volHit else 0)
            + (1 if sprHit else 0) + (1 if cPos[i] >= scCloseMin else 0)
            + (1 if exceptional else 0)
        )
        bcSc = (
            (1 if upTrend else 0) + (1 if newHigh[i] else 0) + (1 if volHit else 0)
            + (1 if sprHit else 0) + (1 if cPos[i] <= bcCloseMax else 0)
            + (1 if exceptional else 0)
        )
        seedOpen = s.phase == PHASE_NONE or s.phase == PHASE_E
        canSC = seedOpen or (
            s.phase == PHASE_A and s.stopSide == DIR_ACCUM and _lt(lows[i], s.climaxPrice)
        )
        canBC = seedOpen or (
            s.phase == PHASE_A and s.stopSide == DIR_DIST and _gt(highs[i], s.climaxPrice)
        )
        isSC = bool(canSC and downTrend and newLow[i] and volHit and sprHit and scSc >= climaxMinScore)
        isBC = bool(canBC and upTrend and newHigh[i] and volHit and sprHit and bcSc >= climaxMinScore)
        if isSC != isBC:
            seedSide = DIR_ACCUM if isSC else DIR_DIST
            recentMarkup = bool(
                regime == REGIME_MARKUP and regimeBar is not None
                and (i - regimeBar) <= ctxBars
            )
            recentMarkdown = bool(
                regime == REGIME_MARKDOWN and regimeBar is not None
                and (i - regimeBar) <= ctxBars
            )
            s = _WS()
            _seed(
                s, seedSide, scSc if isSC else bcSc, preLen[i], trendScore,
                recentMarkup or longBull[i], recentMarkdown or longBear[i],
                a[i], eff[i], spr[i], i, times[i], lows[i], highs[i],
            )
            if (prelimDir == seedSide and prelimBar is not None
                    and prelimBar < i and (i - prelimBar) <= extremeLen):
                s.psTime, s.psPrice, s.psScore = prelimTime, prelimPrice, prelimScore

        # ---- 确认停止动作（吸收）并构筑 AR（原版第 1278–1312 行） ----
        if s.phase == PHASE_A and not s.absorbed and s.climaxBar is not None:
            absAge = i - s.climaxBar
            if climaxAbsorptionMinBars <= absAge <= climaxAbsorptionMaxBars:
                effortOK = _ge(_at(recentEff, i), avgEff[i] * climaxAbsorptionVolFloor)
                accAbs = (
                    s.stopSide == DIR_ACCUM
                    and _ge(_at(recentLow, i), _off(s.climaxPrice, -climaxAbsorptionExtremeATR, s.climaxATR))
                    and _ge(closes[i], _off(s.climaxPrice, climaxAbsorptionReboundATR, s.climaxATR))
                )
                dstAbs = (
                    s.stopSide == DIR_DIST
                    and _le(_at(recentHigh, i), _off(s.climaxPrice, climaxAbsorptionExtremeATR, s.climaxATR))
                    and _le(closes[i], _off(s.climaxPrice, -climaxAbsorptionReboundATR, s.climaxATR))
                )
                if effortOK and (accAbs or dstAbs):
                    s.absorbed = True
                    s.cfClimax = True
                    s.lastActBar = i
                    sig = _add_bit(sig, BIT_SC if s.stopSide == DIR_ACCUM else BIT_BC)
                    if s.psTime is not None:
                        sig = _add_bit(sig, BIT_PS)

        if (s.phase == PHASE_A and s.arBar is None and s.climaxBar is not None
                and i > s.climaxBar and (i - s.climaxBar) <= maxARBars):
            if s.stopSide == DIR_ACCUM:
                if s.arRunPrice is None or highs[i] > s.arRunPrice:
                    s.arRunPrice, s.arRunBar, s.arRunTime = highs[i], i, times[i]
                if not s.arOK:
                    d = (s.arRunPrice - s.climaxPrice) if (
                        s.arRunPrice is not None and s.climaxPrice is not None
                    ) else None
                    if _ge(d, a[i] * minARATR):
                        s.arOK = True
            else:
                if s.arRunPrice is None or lows[i] < s.arRunPrice:
                    s.arRunPrice, s.arRunBar, s.arRunTime = lows[i], i, times[i]
                if not s.arOK:
                    d = (s.climaxPrice - s.arRunPrice) if (
                        s.arRunPrice is not None and s.climaxPrice is not None
                    ) else None
                    if _ge(d, a[i] * minARATR):
                        s.arOK = True
        if s.phase == PHASE_A and s.arOK and s.absorbed and s.arBar is None:
            if s.stopSide == DIR_ACCUM:
                s.rangeHigh = s.arRunPrice
                s.origHigh = s.arRunPrice
            else:
                s.rangeLow = s.arRunPrice
                s.origLow = s.arRunPrice

        # ---- 枢轴驱动的测试（原版第 1314–1463 行） ----
        p1 = _nz(s.pivLen, liveLen[i])
        rk1 = s.rangeHigh is not None and s.rangeLow is not None
        rH1 = s.rangeHigh
        rL1 = s.rangeLow
        origHt1 = (
            max(_nz(s.origHigh, rH1) - _nz(s.origLow, rL1), mintick) if rk1 else None
        )
        mature1 = _mature_ws(s, p1, a[i], i, mintick)
        # 注意：pivBar / pl / ph 用的是**本 bar 顶部**算出的值（原版同此）
        newPL = pl is not None and (lastPLBar is None or pivBar > lastPLBar)
        newPH = ph is not None and (lastPHBar is None or pivBar > lastPHBar)
        if newPL:
            lastPLBar = pivBar
        if newPH:
            lastPHBar = pivBar
        plUsed = False
        phUsed = False

        # 终端测试：低点方向（原版第 1329–1349 行）
        if newPL and s.phase == PHASE_B and mature1 and rk1:
            loRefST = s.stopSide == DIR_ACCUM
            tRefN = s.stN if loRefST else s.opN
            tRefBar = s.stBar if loRefST else s.opLastBar
            tRefPrice = s.stPrice if loRefST else s.opLastPrice
            if tRefN > 0 and tRefBar is not None:
                tRefEff = (s.stEffSum if loRefST else s.opEffSum) / tRefN
                tRefSpr = (s.stSprSum if loRefST else s.opSprSum) / tRefN
                tLater = pivBar >= tRefBar + terminalTestMinBarsAfterST
                tNear = pl <= rL1 + pA * testTolATR
                tHolds = pl >= min(_nz(s.origLow, rL1), rL1) - pA * testExtremeToleranceATR
                tHigher = tRefPrice is None or pl >= tRefPrice - pA * testExtremeToleranceATR
                tqV = pEff <= tRefEff * terminalTestVolRatio
                tqS = pSpr <= tRefSpr * terminalTestSpreadRatio
                tnV = pEff <= tRefEff * terminalTestOtherMaxRatio
                tnS = pSpr <= tRefSpr * terminalTestOtherMaxRatio
                tSc = (1 if tqV else 0) + (1 if tqS else 0) + (1 if pCPos >= 0.50 else 0)
                if (tLater and tNear and tHolds and tHigher
                        and ((tqV and tnS) or (tqS and tnV)) and tSc >= testMinScore):
                    _enter_ctest(s, DIR_ACCUM, pTime, pl, tSc, i, closes[i])
                    sig = _add_bit(sig, BIT_CTEST)
                    plUsed = True
        # 终端测试：高点方向（原版第 1350–1370 行）
        if newPH and s.phase == PHASE_B and mature1 and rk1:
            hiRefST = s.stopSide == DIR_DIST
            uRefN = s.stN if hiRefST else s.opN
            uRefBar = s.stBar if hiRefST else s.opLastBar
            uRefPrice = s.stPrice if hiRefST else s.opLastPrice
            if uRefN > 0 and uRefBar is not None:
                uRefEff = (s.stEffSum if hiRefST else s.opEffSum) / uRefN
                uRefSpr = (s.stSprSum if hiRefST else s.opSprSum) / uRefN
                uLater = pivBar >= uRefBar + terminalTestMinBarsAfterST
                uNear = ph >= rH1 - pA * testTolATR
                uHolds = ph <= max(_nz(s.origHigh, rH1), rH1) + pA * testExtremeToleranceATR
                uLower = uRefPrice is None or ph <= uRefPrice + pA * testExtremeToleranceATR
                uqV = pEff <= uRefEff * terminalTestVolRatio
                uqS = pSpr <= uRefSpr * terminalTestSpreadRatio
                unV = pEff <= uRefEff * terminalTestOtherMaxRatio
                unS = pSpr <= uRefSpr * terminalTestOtherMaxRatio
                uSc = (1 if uqV else 0) + (1 if uqS else 0) + (1 if pCPos <= 0.50 else 0)
                if (uLater and uNear and uHolds and uLower
                        and ((uqV and unS) or (uqS and unV)) and uSc >= testMinScore):
                    _enter_ctest(s, DIR_DIST, pTime, ph, uSc, i, closes[i])
                    sig = _add_bit(sig, BIT_CTEST)
                    phUsed = True

        # Phase A → B：低点方向的 AR 冻结 + 首次 ST（原版第 1372–1391 行）
        if (newPL and not plUsed and s.phase == PHASE_A and s.stopSide == DIR_ACCUM
                and s.absorbed and s.arOK and s.arRunBar is not None
                and pivBar > s.arRunBar):
            aZoneTop = _nz(s.origLow) + (_nz(s.arRunPrice) - _nz(s.origLow)) * phaseBZoneFrac
            aNearL = abs(pl - _nz(s.origLow)) <= pA * boundaryTolATR or pl <= aZoneTop
            aHoldsL = pl >= _nz(s.origLow) - pA * boundaryTolATR
            aCloseL = pCPos >= 0.45
            aTcL = _test_class(aNearL, aHoldsL, aCloseL, pEff, pSpr, s.climaxEff, s.climaxSpr)
            aScL = (
                (1 if aNearL else 0)
                + (1 if _le(pEff, s.climaxEff * stMaxVolRatio if s.climaxEff is not None else None) else 0)
                + (1 if _le(pSpr, s.climaxSpr * stMaxSpreadRatio if s.climaxSpr is not None else None) else 0)
                + (1 if aHoldsL else 0) + (1 if aCloseL else 0) + 1
            )
            aAgeL = s.startBar is not None and (pivBar - s.startBar) >= _phase_a_min_bars(p1)
            aSpaceL = (pivBar - s.arRunBar) >= max(2, p1)
            if aNearL:
                s.lastActBar = i
            if aTcL == TEST_GOOD and aScL >= stMinScore and aAgeL and aSpaceL:
                _freeze_ar(s)
                _register_st(s, pl, pTime, pivBar, pEff, pSpr, aScL)
                s.phase = PHASE_B
                s.bStartBar = pivBar
                s.bStartTime = pTime
                sig = _add_bit(sig, BIT_AR)
                sig = _add_bit(sig, BIT_ST)
                plUsed = True
        # Phase A → B：高点方向（原版第 1392–1411 行）
        if (newPH and not phUsed and s.phase == PHASE_A and s.stopSide == DIR_DIST
                and s.absorbed and s.arOK and s.arRunBar is not None
                and pivBar > s.arRunBar):
            aZoneBot = _nz(s.origHigh) - (_nz(s.origHigh) - _nz(s.arRunPrice)) * phaseBZoneFrac
            aNearH = abs(ph - _nz(s.origHigh)) <= pA * boundaryTolATR or ph >= aZoneBot
            aHoldsH = ph <= _nz(s.origHigh) + pA * boundaryTolATR
            aCloseH = pCPos <= 0.55
            aTcH = _test_class(aNearH, aHoldsH, aCloseH, pEff, pSpr, s.climaxEff, s.climaxSpr)
            aScH = (
                (1 if aNearH else 0)
                + (1 if _le(pEff, s.climaxEff * stMaxVolRatio if s.climaxEff is not None else None) else 0)
                + (1 if _le(pSpr, s.climaxSpr * stMaxSpreadRatio if s.climaxSpr is not None else None) else 0)
                + (1 if aHoldsH else 0) + (1 if aCloseH else 0) + 1
            )
            aAgeH = s.startBar is not None and (pivBar - s.startBar) >= _phase_a_min_bars(p1)
            aSpaceH = (pivBar - s.arRunBar) >= max(2, p1)
            if aNearH:
                s.lastActBar = i
            if aTcH == TEST_GOOD and aScH >= stMinScore and aAgeH and aSpaceH:
                _freeze_ar(s)
                _register_st(s, ph, pTime, pivBar, pEff, pSpr, aScH)
                s.phase = PHASE_B
                s.bStartBar = pivBar
                s.bStartTime = pTime
                sig = _add_bit(sig, BIT_AR)
                sig = _add_bit(sig, BIT_ST)
                phUsed = True

        # Phase B 内的测试 / 对侧测试（原版第 1413–1463 行）
        rk1 = s.rangeHigh is not None and s.rangeLow is not None
        if newPL and not plUsed and s.phase == PHASE_B and rk1 and s.bStartBar is not None and pivBar > s.bStartBar:
            bHt = max(s.rangeHigh - s.rangeLow, mintick)
            if s.stopSide == DIR_ACCUM:
                bNear = (
                    abs(pl - s.rangeLow) <= pA * boundaryTolATR
                    or abs(pl - _nz(s.origLow)) <= pA * boundaryTolATR
                    or pl <= s.rangeLow + bHt * phaseBZoneFrac
                )
                bHolds = pl >= _nz(s.origLow) - pA * boundaryTolATR
                bClose = pCPos >= 0.45
                bTc = _test_class(bNear, bHolds, bClose, pEff, pSpr, s.climaxEff, s.climaxSpr)
                bSc = (
                    (1 if bNear else 0)
                    + (1 if _le(pEff, s.climaxEff * stMaxVolRatio if s.climaxEff is not None else None) else 0)
                    + (1 if _le(pSpr, s.climaxSpr * stMaxSpreadRatio if s.climaxSpr is not None else None) else 0)
                    + (1 if bHolds else 0) + (1 if bClose else 0) + 1
                )
                bIsProv = bool(
                    s.provEdge == DIR_ACCUM and s.provBar is not None
                    and abs(pivBar - s.provBar) <= p1
                )
                if bNear:
                    s.lastActBar = i
                if bTc == TEST_FAILED and not bIsProv:
                    s.lastBadBar = pivBar
                elif bTc == TEST_GOOD and bSc >= stMinScore:
                    _register_st(s, pl, pTime, pivBar, pEff, pSpr, bSc)
                    sig = _add_bit(sig, BIT_ST)
            else:
                if pl < s.rangeLow and pl >= _nz(s.origLow, s.rangeLow) - origHt1 * phaseBEdgeExpansionCap:
                    s.rangeLow = pl
                oHtL = max(s.rangeHigh - s.rangeLow, mintick)
                oNearL = pl <= s.rangeLow + pA * boundaryTolATR or pl <= s.rangeLow + oHtL * phaseBZoneFrac
                oInsideL = pl >= _nz(s.origLow, s.rangeLow) - origHt1 * phaseBEdgeExpansionCap
                oCtrlL = _le(pEff, s.climaxEff * 1.20 if s.climaxEff is not None else None) and _le(
                    pSpr, s.climaxSpr * 1.20 if s.climaxSpr is not None else None
                )
                if oNearL and oInsideL and oCtrlL and (s.opLastBar is None or pivBar > s.opLastBar):
                    _register_opp(s, pl, pTime, pivBar, pEff, pSpr, i)
        if newPH and not phUsed and s.phase == PHASE_B and rk1 and s.bStartBar is not None and pivBar > s.bStartBar:
            bHtH = max(s.rangeHigh - s.rangeLow, mintick)
            if s.stopSide == DIR_DIST:
                hNear = (
                    abs(ph - s.rangeHigh) <= pA * boundaryTolATR
                    or abs(ph - _nz(s.origHigh)) <= pA * boundaryTolATR
                    or ph >= s.rangeHigh - bHtH * phaseBZoneFrac
                )
                hHolds = ph <= _nz(s.origHigh) + pA * boundaryTolATR
                hClose = pCPos <= 0.55
                hTc = _test_class(hNear, hHolds, hClose, pEff, pSpr, s.climaxEff, s.climaxSpr)
                hSc = (
                    (1 if hNear else 0)
                    + (1 if _le(pEff, s.climaxEff * stMaxVolRatio if s.climaxEff is not None else None) else 0)
                    + (1 if _le(pSpr, s.climaxSpr * stMaxSpreadRatio if s.climaxSpr is not None else None) else 0)
                    + (1 if hHolds else 0) + (1 if hClose else 0) + 1
                )
                hIsProv = bool(
                    s.provEdge == DIR_DIST and s.provBar is not None
                    and abs(pivBar - s.provBar) <= p1
                )
                if hNear:
                    s.lastActBar = i
                if hTc == TEST_FAILED and not hIsProv:
                    s.lastBadBar = pivBar
                elif hTc == TEST_GOOD and hSc >= stMinScore:
                    _register_st(s, ph, pTime, pivBar, pEff, pSpr, hSc)
                    sig = _add_bit(sig, BIT_ST)
            else:
                if ph > s.rangeHigh and ph <= _nz(s.origHigh, s.rangeHigh) + origHt1 * phaseBEdgeExpansionCap:
                    s.rangeHigh = ph
                oHtH = max(s.rangeHigh - s.rangeLow, mintick)
                oNearH = ph >= s.rangeHigh - pA * boundaryTolATR or ph >= s.rangeHigh - oHtH * phaseBZoneFrac
                oInsideH = ph <= _nz(s.origHigh, s.rangeHigh) + origHt1 * phaseBEdgeExpansionCap
                oCtrlH = _le(pEff, s.climaxEff * 1.20 if s.climaxEff is not None else None) and _le(
                    pSpr, s.climaxSpr * 1.20 if s.climaxSpr is not None else None
                )
                if oNearH and oInsideH and oCtrlH and (s.opLastBar is None or pivBar > s.opLastBar):
                    _register_opp(s, ph, pTime, pivBar, pEff, pSpr, i)

        # ---- 区间穿越计数（原版第 1465–1476 行） ----
        if s.phase == PHASE_B and s.rangeHigh is not None and s.rangeLow is not None:
            tvHt = max(s.rangeHigh - s.rangeLow, mintick)
            loZoneP = newPL and pl <= s.rangeLow + tvHt * phaseBZoneFrac
            hiZoneP = newPH and ph >= s.rangeHigh - tvHt * phaseBZoneFrac
            if loZoneP != hiZoneP:
                zoneNow = -1 if loZoneP else 1
            else:
                zoneNow = 0
            if zoneNow != 0 and (s.lastZoneBar is None or pivBar > s.lastZoneBar):
                if s.lastZone != 0 and zoneNow != s.lastZone:
                    s.travCount += 1
                    s.lastActBar = i
                s.lastZone = zoneNow
                s.lastZoneBar = pivBar

        # ---- Phase C：Spring/UTAD 之后的重测（原版第 1478–1503 行） ----
        if (newPL and s.phase == PHASE_C and s.outcome == DIR_ACCUM and s.exc
                and not s.excTested and s.excBar is not None and pivBar > s.excBar
                and s.rangeLow is not None):
            xNearL = pl <= s.rangeLow + pA * testTolATR
            xKeepL = _ge(pl, _off(s.excPrice, -testExtremeToleranceATR, pA))
            xScL = (
                (1 if _le(pEff, s.excEff * testMaxVolRatio if s.excEff is not None else None) else 0)
                + (1 if _le(pSpr, s.excSpr * testMaxSpreadRatio if s.excSpr is not None else None) else 0)
                + (1 if pCPos >= 0.50 else 0)
            )
            if xNearL and xKeepL and xScL >= testMinScore:
                s.excTested = True
                s.cReadyBar = i
                s.testTime, s.testPrice, s.testScore = pTime, pl, xScL
                s.cfTest = True
                _set_event(s, EV_TEST, i, times[i], closes[i])
                sig = _add_bit(sig, BIT_TEST)
        if (newPH and s.phase == PHASE_C and s.outcome == DIR_DIST and s.exc
                and not s.excTested and s.excBar is not None and pivBar > s.excBar
                and s.rangeHigh is not None):
            xNearH = ph >= s.rangeHigh - pA * testTolATR
            xKeepH = _le(ph, _off(s.excPrice, testExtremeToleranceATR, pA))
            xScH = (
                (1 if _le(pEff, s.excEff * testMaxVolRatio if s.excEff is not None else None) else 0)
                + (1 if _le(pSpr, s.excSpr * testMaxSpreadRatio if s.excSpr is not None else None) else 0)
                + (1 if pCPos <= 0.50 else 0)
            )
            if xNearH and xKeepH and xScH >= testMinScore:
                s.excTested = True
                s.cReadyBar = i
                s.testTime, s.testPrice, s.testScore = pTime, ph, xScH
                s.cfTest = True
                _set_event(s, EV_TEST, i, times[i], closes[i])
                sig = _add_bit(sig, BIT_TEST)

        # ---- Phase D：LPS / LPSY（原版第 1505–1530 行） ----
        if (newPL and s.phase == PHASE_D and s.outcome == DIR_ACCUM
                and s.strBar is not None and pivBar > s.strBar and s.rangeHigh is not None):
            lHard = _hard_level(s, DIR_ACCUM)
            lSide = lHard is not None and pl > lHard - pA * testExtremeToleranceATR
            lCreek = pl >= s.rangeHigh - pA * lpsBoundaryATR
            lSc = (
                (1 if _le(pEff, pAvgEff * lpsVolMult) else 0)
                + (1 if _le(pSpr, pA * lpsSpreadATR) else 0)
                + (1 if pCPos >= 0.45 else 0)
            )
            if lSide and lCreek and lSc >= lpsMinScore:
                s.lpsTime, s.lpsPrice, s.lpsBar, s.lpsScore = pTime, pl, i, lSc
                s.cfLPS = True
                _set_event(s, EV_LPS, i, times[i], closes[i])
                sig = _add_bit(sig, BIT_LPS)
        if (newPH and s.phase == PHASE_D and s.outcome == DIR_DIST
                and s.strBar is not None and pivBar > s.strBar and s.rangeLow is not None):
            yHard = _hard_level(s, DIR_DIST)
            ySide = yHard is not None and ph < yHard + pA * testExtremeToleranceATR
            yIce = ph <= s.rangeLow + pA * lpsBoundaryATR
            ySc = (
                (1 if _le(pEff, pAvgEff * lpsVolMult) else 0)
                + (1 if _le(pSpr, pA * lpsSpreadATR) else 0)
                + (1 if pCPos <= 0.55 else 0)
            )
            if ySide and yIce and ySc >= lpsMinScore:
                s.lpsTime, s.lpsPrice, s.lpsBar, s.lpsScore = pTime, ph, i, ySc
                s.cfLPS = True
                _set_event(s, EV_LPSY, i, times[i], closes[i])
                sig = _add_bit(sig, BIT_LPSY)

        # ---- Spring / UTAD 探测（原版第 1532–1597 行） ----
        p2 = _nz(s.pivLen, liveLen[i])
        rk2 = s.rangeHigh is not None and s.rangeLow is not None
        rH2 = s.rangeHigh
        rL2 = s.rangeLow
        recLimit2 = _excursion_recovery_limit(p2)
        mature2 = _mature_ws(s, p2, a[i], i, mintick)
        bAge2 = (i - s.bStartBar) if s.bStartBar is not None else 0
        probeB = s.phase == PHASE_B and s.stCount >= 1 and bAge2 >= max(3, p2 * 2)
        probeCLo = s.phase == PHASE_C and s.outcome == DIR_ACCUM and not s.exc
        probeCHi = s.phase == PHASE_C and s.outcome == DIR_DIST and not s.exc

        if rk2 and not s.pend and (s.pendCoolBar is None or i > s.pendCoolBar):
            penLo = rL2 - lows[i]
            penHi = highs[i] - rH2
            okLo = bool(
                (probeB or probeCLo) and penLo >= a[i] * springMinPenATR
                and penLo <= a[i] * springMaxPenATR
            )
            okHi = bool(
                (probeB or probeCHi) and penHi >= a[i] * springMinPenATR
                and penHi <= a[i] * springMaxPenATR
            )
            if okLo != okHi:
                s.pend = True
                s.pendEdge = DIR_ACCUM if okLo else DIR_DIST
                s.pendStartBar = i
                s.pendExtBar = i
                s.pendExt = lows[i] if okLo else highs[i]
                s.pendExtTime = times[i]
                s.pendEff = eff[i]
                s.pendSpr = spr[i]

        if s.pend and rk2 and (s.phase == PHASE_B or s.phase == PHASE_C):
            if s.pendEdge == DIR_ACCUM and lows[i] < _nz(s.pendExt):
                s.pendExt, s.pendExtBar, s.pendExtTime = lows[i], i, times[i]
                s.pendEff = max(_nz(s.pendEff), eff[i])
                s.pendSpr = max(_nz(s.pendSpr), spr[i])
            elif s.pendEdge == DIR_DIST and highs[i] > _nz(s.pendExt):
                s.pendExt, s.pendExtBar, s.pendExtTime = highs[i], i, times[i]
                s.pendEff = max(_nz(s.pendEff), eff[i])
                s.pendSpr = max(_nz(s.pendSpr), spr[i])
            recAge = i - _nz(s.pendStartBar, i)
            recovered = (
                closes[i] > rL2 if s.pendEdge == DIR_ACCUM else closes[i] < rH2
            )
            depth = (
                rL2 - _nz(s.pendExt) if s.pendEdge == DIR_ACCUM else _nz(s.pendExt) - rH2
            )
            depthOK = depth >= a[i] * springMinPenATR and depth <= a[i] * springMaxPenATR
            if s.pendEdge == DIR_ACCUM:
                recSc = (
                    (1 if cPos[i] >= springCloseMin else 0)
                    + (1 if eff[i] <= avgEff[i] * springEffortMaxMult else 0)
                    + (1 if closes[i] > opens[i] else 0)
                    + (1 if spr[i] >= a[i] * 0.50 else 0)
                )
            else:
                recSc = (
                    (1 if cPos[i] <= utadCloseMax else 0)
                    + (1 if eff[i] <= avgEff[i] * springEffortMaxMult else 0)
                    + (1 if closes[i] < opens[i] else 0)
                    + (1 if spr[i] >= a[i] * 0.50 else 0)
                )
            if recovered and depthOK and recAge <= recLimit2 and recSc >= excursionMinScore:
                probeEdge = s.pendEdge
                if s.phase == PHASE_B and not mature2:
                    # 成因未足：先存成「临时 Spring/UTAD」，等后续确认（原版 prov*）
                    s.provEdge = probeEdge
                    s.provBar = s.pendExtBar
                    s.provPrice = s.pendExt
                    s.provTime = s.pendExtTime
                    s.provEff = s.pendEff
                    s.provSpr = s.pendSpr
                    s.provScore = recSc
                    s.lastActBar = i
                    s.pend = False
                else:
                    if s.phase == PHASE_C:
                        _clear_terminal(s)
                    _exc_from_pend(s, recSc, i, times[i], closes[i])
                    sig = _add_bit(sig, BIT_SPRING if probeEdge == DIR_ACCUM else BIT_UTAD)
            elif recAge > recLimit2 or ((not depthOK) and depth > a[i] * springMaxPenATR):
                if s.phase == PHASE_B:
                    s.lastBadBar = i
                s.pend = False
                s.pendCoolBar = i + p2

        # ---- Spring/UTAD 后继续探深（原版第 1599–1611 行） ----
        if s.phase == PHASE_C and s.exc and not s.excTested and rk2:
            if (s.outcome == DIR_ACCUM and lows[i] < _nz(s.excPrice)
                    and lows[i] >= rL2 - a[i] * springMaxPenATR):
                s.excPrice, s.excBar, s.excTime = lows[i], i, times[i]
                s.excEff = max(_nz(s.excEff), eff[i])
                s.excSpr = max(_nz(s.excSpr), spr[i])
            elif (s.outcome == DIR_DIST and highs[i] > _nz(s.excPrice)
                    and highs[i] <= rH2 + a[i] * springMaxPenATR):
                s.excPrice, s.excBar, s.excTime = highs[i], i, times[i]
                s.excEff = max(_nz(s.excEff), eff[i])
                s.excSpr = max(_nz(s.excSpr), spr[i])

        # ---- SOS / SOW：推进到 Phase D（原版第 1613–1642 行） ----
        if s.phase == PHASE_C and rk2 and not s.pend:
            cMinD = _phase_c_to_d_min_bars(p2)
            cFormal = (not s.exc) or s.excTested
            cAnchor = s.cReadyBar if s.cReadyBar is not None else s.excBar
            cAgeReady = cAnchor is not None and (i - cAnchor) >= cMinD
            cVal = _validation_ws(s, p2, trendScore, a[i], i, mintick)
            cValD = cVal if cFormal else min(100, cVal + 13)
            cConf = _conf_ws(s)
            cConfD = cConf if cFormal else min(100, cConf + 30)
            cHt = max(rH2 - rL2, mintick)
            if cAgeReady and cValD >= phaseDValidationMin and cConfD >= minConfidencePhaseC:
                if s.outcome == DIR_ACCUM and ageOKBull:
                    domUp = rL2 + cHt * phaseDDominanceFrac
                    brkUp = closes[i] > rH2 + a[i] * breakATR
                    domOKUp = brkUp or (
                        closes[i] >= domUp and close1[i] >= domUp
                        if cFormal else
                        closes[i] >= domUp and close1[i] >= domUp and close2[i] >= domUp
                    )
                    reqUp = strengthMinScore if cFormal else max(2, strengthMinScore - 1)
                    qUp = sosScore[i] >= reqUp or mbSOS[i] or (
                        (not cFormal) and res3Up[i] >= 1.0
                    )
                    if domOKUp and qUp:
                        _promote_d(s, DIR_ACCUM, sosScore[i], highs[i], i, times[i], closes[i])
                        sig = _add_bit(sig, BIT_SOS)
                elif s.outcome == DIR_DIST and ageOKBear:
                    domDn = rH2 - cHt * phaseDDominanceFrac
                    brkDn = closes[i] < rL2 - a[i] * breakATR
                    domOKDn = brkDn or (
                        closes[i] <= domDn and close1[i] <= domDn
                        if cFormal else
                        closes[i] <= domDn and close1[i] <= domDn and close2[i] <= domDn
                    )
                    reqDn = strengthMinScore if cFormal else max(2, strengthMinScore - 1)
                    qDn = sowScore[i] >= reqDn or mbSOW[i] or (
                        (not cFormal) and res3Dn[i] >= 1.0
                    )
                    if domOKDn and qDn:
                        _promote_d(s, DIR_DIST, sowScore[i], lows[i], i, times[i], closes[i])
                        sig = _add_bit(sig, BIT_SOW)

        # ---- Phase B 直接离场（未确认）→ 重置或借临时探针直推 Phase D（原版第 1644–1677 行） ----
        resetLate = -1
        if s.phase == PHASE_B and rk2 and not s.pend:
            dHt = max(rH2 - rL2, mintick)
            dBuf = max(dHt * phaseBDepartureTRFrac, a[i] * phaseBDepartureATR)
            upAcc = closes[i] > rH2 + a[i] * breakATR and close1[i] > rH2 + a[i] * breakATR
            dnAcc = closes[i] < rL2 - a[i] * breakATR and close1[i] < rL2 - a[i] * breakATR
            upSus = _gt(_at(loCDep, i), _off(rH2, 1.0, dBuf))
            dnSus = _lt(_at(hiCDep, i), _off(rL2, -1.0, dBuf))
            upGo = (upAcc and (sosScore[i] >= strengthMinScore or mbSOS[i])) or upSus
            dnGo = (dnAcc and (sowScore[i] >= strengthMinScore or mbSOW[i])) or dnSus
            provLimit = _phase_idle_limit(PHASE_C, True, p2)
            if upGo and not dnGo:
                upProv = bool(
                    s.provEdge == DIR_ACCUM and s.provBar is not None
                    and (i - s.provBar) <= _nz(provLimit, 0)
                )
                if ageOKBull and upProv and mature2:
                    _adopt_prov(s)
                    _promote_d(s, DIR_ACCUM, sosScore[i], highs[i], i, times[i], closes[i])
                    sig = _add_bit(sig, BIT_SPRING)
                    sig = _add_bit(sig, BIT_SOS)
                elif upSus and not upProv:
                    resetLate = RS_DEPART
            elif dnGo and not upGo:
                dnProv = bool(
                    s.provEdge == DIR_DIST and s.provBar is not None
                    and (i - s.provBar) <= _nz(provLimit, 0)
                )
                if ageOKBear and dnProv and mature2:
                    _adopt_prov(s)
                    _promote_d(s, DIR_DIST, sowScore[i], lows[i], i, times[i], closes[i])
                    sig = _add_bit(sig, BIT_UTAD)
                    sig = _add_bit(sig, BIT_SOW)
                elif dnSus and not dnProv:
                    resetLate = RS_DEPART
        if resetLate >= 0:
            rs_counts[resetLate] += 1
            last_why = resetLate
            s = _WS()

        # ---- Phase D 内的力量重估（原版第 1679–1694 行） ----
        if (s.phase == PHASE_D and s.outcome == DIR_ACCUM and s.ev == EV_LPS
                and s.rangeHigh is not None):
            if (closes[i] > s.rangeHigh + a[i] * breakATR
                    and relE[i] >= strengthVolMult and cPos[i] >= sosCloseMin):
                s.strBar, s.strTime, s.strPrice, s.strScore = i, times[i], highs[i], sosScore[i]
                _set_event(s, EV_SOS, i, times[i], closes[i])
                sig = _add_bit(sig, BIT_SOS)
        if (s.phase == PHASE_D and s.outcome == DIR_DIST and s.ev == EV_LPSY
                and s.rangeLow is not None):
            if (closes[i] < s.rangeLow - a[i] * breakATR
                    and relE[i] >= strengthVolMult and cPos[i] <= sowCloseMax):
                s.strBar, s.strTime, s.strPrice, s.strScore = i, times[i], lows[i], sowScore[i]
                _set_event(s, EV_SOW, i, times[i], closes[i])
                sig = _add_bit(sig, BIT_SOW)

        # ---- 接受突破 → Phase E（原版第 1696–1728 行） ----
        if s.phase == PHASE_D and s.rangeHigh is not None and s.rangeLow is not None:
            eConf = _conf_ws(s)
            dAge = (i - s.dStartBar) if s.dStartBar is not None else 0
            dMin = _phase_d_min_bars(_nz(s.pivLen, liveLen[i]))
            if s.outcome == DIR_ACCUM:
                s.outCount = s.outCount + 1 if closes[i] > s.rangeHigh else 0
                eOppOKU = regime != REGIME_MARKDOWN or s.cfLPS
                eStdU = bool(
                    s.cfLPS and s.lpsBar is not None and (i - s.lpsBar) >= confirmBars
                    and dAge >= dMin and _gt(_at(loCConf, i), s.rangeHigh)
                    and eConf >= minConfidencePhaseD
                )
                eDirU = bool(
                    (not s.cfLPS) and dAge >= max(dMin, directAcceptanceBars)
                    and _gt(_at(loCDir, i), s.rangeHigh)
                    and eConf >= directAcceptanceConfidence
                )
                if (eStdU or eDirU) and eOppOKU:
                    s.cfAccept = True
                    s.phase = PHASE_E
                    s.eStartBar, s.eStartTime = i, times[i]
                    regime, regimeBar = REGIME_MARKUP, i
                    _set_event(s, EV_MARKUP, i, times[i], closes[i])
                    sig = _add_bit(sig, BIT_E)
            elif s.outcome == DIR_DIST:
                s.outCount = s.outCount + 1 if closes[i] < s.rangeLow else 0
                eOppOKD = regime != REGIME_MARKUP or s.cfLPS
                eStdD = bool(
                    s.cfLPS and s.lpsBar is not None and (i - s.lpsBar) >= confirmBars
                    and dAge >= dMin and _lt(_at(hiCConf, i), s.rangeLow)
                    and eConf >= minConfidencePhaseD
                )
                eDirD = bool(
                    (not s.cfLPS) and dAge >= max(dMin, directAcceptanceBars)
                    and _lt(_at(hiCDir, i), s.rangeLow)
                    and eConf >= directAcceptanceConfidence
                )
                if (eStdD or eDirD) and eOppOKD:
                    s.cfAccept = True
                    s.phase = PHASE_E
                    s.eStartBar, s.eStartTime = i, times[i]
                    regime, regimeBar = REGIME_MARKDOWN, i
                    _set_event(s, EV_MARKDOWN, i, times[i], closes[i])
                    sig = _add_bit(sig, BIT_E)

        # ---- 入场信号（原版第 1730–1748 行） ----
        if s.entryTime is None and s.outcome != DIR_NONE:
            if strictness == "Conservative":
                eMinConf, eMinTests = entryMinConfidenceConservative, entryMinTestsConservative
                eValFloor = phaseDValidationMin
            elif strictness == "Standard":
                eMinConf, eMinTests = entryMinConfidenceStandard, entryMinTestsStandard
                eValFloor = max(45, phaseDValidationMin - 10)
            else:
                eMinConf, eMinTests = entryMinConfidenceAggressive, entryMinTestsAggressive
                eValFloor = max(45, phaseDValidationMin - 10)
            eVal = _validation_ws(s, _nz(s.pivLen, liveLen[i]), trendScore, a[i], i, mintick)
            eReady = _entry_readiness_ws(
                s, eMinConf, eValFloor, a[i], i, closes[i], times[i], tf_secs, mintick,
            )
            eQuality = _conf_ws(s) >= eMinConf and eVal >= eValFloor and eReady >= eMinTests
            testBit = _has_bit(sig, BIT_TEST) or _has_bit(sig, BIT_CTEST)
            strengthBit = _has_bit(sig, BIT_SOS) or _has_bit(sig, BIT_SOW)
            lpsBit = _has_bit(sig, BIT_LPS) or _has_bit(sig, BIT_LPSY)
            aggressiveTestTrig = strictness == "Aggressive" and testBit and eQuality
            aggressiveStrengthFallback = (
                strictness == "Aggressive" and strengthBit and eQuality and s.testTime is None
            )
            standardTestTrig = (
                strictness == "Standard" and testBit and eQuality
                and s.testScore >= testMinScore
            )
            standardLpsFallback = strictness == "Standard" and lpsBit and eQuality
            conservativeLpsTrig = strictness == "Conservative" and lpsBit and eQuality
            if (aggressiveTestTrig or aggressiveStrengthFallback or standardTestTrig
                    or standardLpsFallback or conservativeLpsTrig):
                s.entryTime = times[i]
                s.entryPrice = closes[i]
                if standardLpsFallback or conservativeLpsTrig:
                    s.entryKind = ENTRY_LPS
                elif aggressiveStrengthFallback:
                    s.entryKind = ENTRY_STRENGTH
                else:
                    s.entryKind = ENTRY_TEST

    return _snapshot(
        s, i=n - 1, close_i=closes[n - 1], atr_now=a[n - 1], trend_now=trendScore,
        regime=regime, strictness=strictness, mintick=mintick,
        rs_counts=rs_counts, last_why=last_why,
    )


def _tf_secs(times: list[float]) -> float:
    """相邻 bar 时间的中位间距（**秒**），对应原版 `timeframe.in_seconds(period)`。

    `times` 单位是 epoch 秒，故差值本身就是秒，不再 /1000。
    """
    if len(times) < 2:
        return 900.0
    diffs = sorted(
        float(times[k + 1]) - float(times[k])
        for k in range(len(times) - 1)
        if float(times[k + 1]) > float(times[k])
    )
    if not diffs:
        return 900.0
    return diffs[len(diffs) // 2] or 900.0


def _snapshot(
    s: _WS, *, i: int, close_i: float, atr_now: float, trend_now: float,
    regime: int, strictness: str, mintick: float,
    rs_counts: list[int], last_why: int,
) -> dict[str, Any]:
    """把 `f_engine` 末尾的 55 个返回值整理成给 LLM 的快照（原版第 1750–1765 行）。"""
    out_type = _type_ws(s)
    out_wdir = s.outcome if s.outcome != DIR_NONE else _bias_dir(s)
    conf = 0 if s.stopSide == DIR_NONE else _conf_ws(s)
    p = _nz(s.pivLen, pivotLen)
    # f_describe 只在最后一根 bar 上跑（原版第 1760 行 `barstate.islast`），
    # 这里 `s` 就是最后一根 bar 结束时的状态。
    nxt, checks = _describe(s, p, atr_now, rs_counts, last_why, regime, i, mintick)

    events: dict[str, Any] = {}
    if s.climaxTime is not None:
        events["climax"] = {
            "kind": "SC" if s.stopSide == DIR_ACCUM else "BC",
            "time": _iso(s.climaxTime),
            "price": _round(s.climaxPrice),
            "score": s.climaxScore,
        }
    if s.psTime is not None:
        events["preliminary"] = {
            "kind": "PS" if s.stopSide == DIR_ACCUM else "PSY",
            "time": _iso(s.psTime),
            "price": _round(s.psPrice),
            "score": s.psScore,
        }
    if s.arTime is not None:
        events["ar"] = {"time": _iso(s.arTime), "price": _round(s.arPrice)}
    st_list = []
    for t_key, p_key in (("t1", "p1"), ("t2", "p2"), ("t3", "p3")):
        tv = getattr(s.stHist, t_key)
        if tv is not None:
            st_list.append({"time": _iso(tv), "price": _round(getattr(s.stHist, p_key))})
    if st_list:
        events["st"] = st_list
    opp_list = []
    for t_key, p_key in (("t1", "p1"), ("t2", "p2"), ("t3", "p3")):
        tv = getattr(s.opHist, t_key)
        if tv is not None:
            opp_list.append({"time": _iso(tv), "price": _round(getattr(s.opHist, p_key))})
    if opp_list:
        events["opposite_edge_tests"] = opp_list
    if s.excTime is not None:
        events["spring" if s.outcome == DIR_ACCUM else "utad"] = {
            "time": _iso(s.excTime),
            "price": _round(s.excPrice),
            "score": s.excScore,
            "tested": bool(s.excTested),
        }
    if s.testTime is not None:
        events["test"] = {
            "time": _iso(s.testTime), "price": _round(s.testPrice), "score": s.testScore,
        }
    if s.strTime is not None:
        events["strength"] = {
            "kind": "SOS" if s.outcome == DIR_ACCUM else "SOW",
            "time": _iso(s.strTime),
            "price": _round(s.strPrice),
            "score": s.strScore,
        }
    if s.lpsTime is not None:
        events["last_point"] = {
            "kind": "LPS" if s.outcome == DIR_ACCUM else "LPSY",
            "time": _iso(s.lpsTime),
            "price": _round(s.lpsPrice),
            "score": s.lpsScore,
        }

    entry: Optional[dict[str, Any]] = None
    if s.entryTime is not None:
        entry = {
            "kind": _ENTRY_KIND_NAME.get(s.entryKind),
            "time": _iso(s.entryTime),
            "price": _round(s.entryPrice),
            "strictness": strictness,
        }

    return {
        "phase": _PHASE_LETTER.get(s.phase),
        "phase_name": _PHASE_NAME.get(s.phase),
        "structure": _TYPE_NAME.get(out_type),
        "stop_side": {DIR_ACCUM: "accum", DIR_DIST: "dist"}.get(s.stopSide),
        "outcome": {DIR_ACCUM: "accum", DIR_DIST: "dist"}.get(s.outcome),
        "outcome_direction": {DIR_ACCUM: "up", DIR_DIST: "down"}.get(out_wdir),
        "event": _EVENT_NAME.get(s.ev),
        "confidence": conf,
        "validation": _validation_ws(s, p, trend_now, atr_now, i, mintick),
        "range": {
            "high": _round(s.rangeHigh),
            "low": _round(s.rangeLow),
            "height_atr": _round(_range_atr_ws(s, atr_now, mintick), 4),
        },
        "range_origin": (
            "Downtrend halted (SC)" if s.stopSide == DIR_ACCUM
            else "Uptrend halted (BC)" if s.stopSide == DIR_DIST else None
        ),
        "absorbed": bool(s.absorbed),
        "probe": (
            "spring" if s.pendEdge == DIR_ACCUM else "utad" if s.pendEdge == DIR_DIST else None
        ) if s.pend else (
            "spring" if s.provEdge == DIR_ACCUM else "utad" if s.provEdge == DIR_DIST else None
        ),
        "events": events,
        "entry": entry,
        "next": nxt,
        "checks": checks,
        "atr": _round(atr_now, 6),
        "trend_score": _round(s.birthTrend, 2),
    }
