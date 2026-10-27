"""写入端指标引擎（pa-data-source 唯一口径）。

设计约束
- **纯函数**，不依赖 DB / 不 import omnialpha（数据源只单向上游）；
- 数值公式与 ``omnialpha/strategist/indicators.py`` **逐步一致**（同一 SMA 种子、同一 Wilder 递推），
  由 ``tests/test_indicator_core.py`` 的 A/B 对拍钉死（相对误差 ≤1e-9）；
- 提供 ``StreamState``：逐根喂入的增量状态机，结果与批量函数**逐位相等**（测试断言 bit 相同），
  供 WS 收线路径使用，避免每根重算整条序列。
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

Series = list[Optional[float]]

__all__ = [
    "ema",
    "sma",
    "rma",
    "stdev",
    "true_range",
    "atr",
    "rsi",
    "macd",
    "boll",
    "StreamState",
]


# ── 批量（与 strategist/indicators.py 同公式） ───────────────────────
def ema(values: Sequence[float], period: int) -> Series:
    """EMA(k=2/(period+1))，SMA 种子落在 index period-1。"""
    n = len(values)
    out: Series = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def sma(values: Sequence[float], period: int) -> Series:
    n = len(values)
    out: Series = [None] * n
    if period <= 0 or n < period:
        return out
    run = sum(values[:period])
    out[period - 1] = run / period
    for i in range(period, n):
        run += values[i] - values[i - period]
        out[i] = run / period
    return out


def rma(values: Sequence[float], period: int) -> Series:
    """Wilder RMA：种子 = 前 period 个的均值，然后 prev*(n-1)/n + x/n。"""
    n = len(values)
    out: Series = [None] * n
    if period <= 0 or n < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def stdev(values: Sequence[float], period: int) -> Series:
    """滚动**总体**标准差（Pine ta.stdev 默认）。"""
    n = len(values)
    out: Series = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        out[i] = var ** 0.5
    return out


def true_range(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float]) -> list[float]:
    """Pine ta.tr(true)：首根 = h-l，其后 = max(h-l, |h-pc|, |l-pc|)。"""
    trs: list[float] = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        if i == 0:
            trs.append(h - l)
        else:
            pc = closes[i - 1]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14) -> Series:
    return rma(true_range(highs, lows, closes), period)


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """Wilder RSI：首个有效值出现在 index == period（需要 period+1 根）。"""
    n = len(closes)
    out: Series = [None] * n
    if period <= 0 or n <= period:
        return out
    gains: list[float] = []
    losses: list[float] = []
    avg_gain: Optional[float] = None
    avg_loss: Optional[float] = None
    prev = closes[0]
    for i in range(1, n):
        ch = closes[i] - prev
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
        if len(gains) < period:
            prev = closes[i]
            continue
        if avg_gain is None:
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period
        else:
            avg_gain = (avg_gain * (period - 1) + gains[-1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[-1]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
        prev = closes[i]
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict[str, Series]:
    """Pine MACD：dif = ema(fast)-ema(slow)；dea = ema(dif, signal)；hist = dif-dea。"""
    ef, es = ema(closes, fast), ema(closes, slow)
    dif: Series = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    dea: Series = [None] * len(dif)
    k = 2.0 / (signal + 1)
    state: Optional[float] = None
    seed: list[float] = []
    for i, d in enumerate(dif):
        if d is None:
            continue
        if state is None:
            seed.append(d)
            if len(seed) >= signal:
                state = sum(seed) / signal
                dea[i] = state
        else:
            state = d * k + state * (1.0 - k)
            dea[i] = state
    hist: Series = [None if d is None or s is None else d - s for d, s in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "hist": hist}


def boll(closes: Sequence[float], period: int = 20, k: float = 2.0) -> dict[str, Series]:
    """Pine BOLL：basis = SMA(period)，dev 用**总体**标准差，upper/lower = basis ± k·sd。"""
    basis = sma(closes, period)
    sd = stdev(closes, period)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i, (b, s) in enumerate(zip(basis, sd)):
        if b is None or s is None:
            continue
        upper[i] = b + k * s
        lower[i] = b - k * s
    return {"middle": basis, "upper": upper, "lower": lower}


# ── 流式（增量）────────────────────────────────────────────────────
class _EmaState:
    __slots__ = ("period", "k", "buf", "val")

    def __init__(self, period: int) -> None:
        self.period = period
        self.k = 2.0 / (period + 1)
        self.buf: list[float] = []
        self.val: Optional[float] = None

    def push(self, x: float) -> Optional[float]:
        if self.val is None:
            self.buf.append(x)
            if len(self.buf) < self.period:
                return None
            self.val = sum(self.buf) / self.period
            self.buf.clear()
            return self.val
        self.val = x * self.k + self.val * (1.0 - self.k)
        return self.val


class _RmaState:
    __slots__ = ("period", "buf", "val")

    def __init__(self, period: int) -> None:
        self.period = period
        self.buf: list[float] = []
        self.val: Optional[float] = None

    def push(self, x: float) -> Optional[float]:
        if self.val is None:
            self.buf.append(x)
            if len(self.buf) < self.period:
                return None
            self.val = sum(self.buf) / self.period
            self.buf.clear()
            return self.val
        self.val = (self.val * (self.period - 1) + x) / self.period
        return self.val


class _SmaState:
    """与批量 ``sma()`` 逐位一致：种子用内建 ``sum()``（3.12 起为补偿求和），之后朴素滚动。"""

    __slots__ = ("period", "buf", "run", "seeded")

    def __init__(self, period: int) -> None:
        self.period = period
        self.buf: list[float] = []
        self.run = 0.0
        self.seeded = False

    def push(self, x: float) -> Optional[float]:
        self.buf.append(x)
        if not self.seeded:
            if len(self.buf) < self.period:
                return None
            self.run = sum(self.buf)
            self.seeded = True
            return self.run / self.period
        old = self.buf.pop(0)
        self.run += x - old
        return self.run / self.period


class _AtrState:
    __slots__ = ("period", "rma", "prev_close", "first")

    def __init__(self, period: int) -> None:
        self.period = period
        self.rma = _RmaState(period)
        self.prev_close: Optional[float] = None
        self.first = True

    def push(self, h: float, l: float, c: float) -> Optional[float]:
        if self.first:
            tr = h - l
            self.first = False
        else:
            pc = self.prev_close if self.prev_close is not None else c
            tr = max(h - l, abs(h - pc), abs(l - pc))
        self.prev_close = c
        return self.rma.push(tr)


class _RsiState:
    __slots__ = ("period", "prev", "gains", "losses", "avg_gain", "avg_loss")

    def __init__(self, period: int) -> None:
        self.period = period
        self.prev: Optional[float] = None
        self.gains: list[float] = []
        self.losses: list[float] = []
        self.avg_gain: Optional[float] = None
        self.avg_loss: Optional[float] = None

    def push(self, c: float) -> Optional[float]:
        if self.prev is None:
            self.prev = c
            return None
        ch = c - self.prev
        self.prev = c
        g, ls = max(ch, 0.0), max(-ch, 0.0)
        if self.avg_gain is None:
            self.gains.append(g)
            self.losses.append(ls)
            if len(self.gains) < self.period:
                return None
            self.avg_gain = sum(self.gains) / self.period
            self.avg_loss = sum(self.losses) / self.period
            self.gains.clear()
            self.losses.clear()
            return _rsi_value(self.avg_gain, self.avg_loss or 0.0)
        self.avg_gain = (self.avg_gain * (self.period - 1) + g) / self.period
        self.avg_loss = (self.avg_loss * (self.period - 1) + ls) / self.period  # type: ignore[operator]
        return _rsi_value(self.avg_gain, self.avg_loss)


class _MacdState:
    __slots__ = ("fast", "slow", "signal", "ef", "es", "dea", "seed")

    def __init__(self, fast: int, slow: int, signal: int) -> None:
        self.fast, self.slow, self.signal = fast, slow, signal
        self.ef, self.es = _EmaState(fast), _EmaState(slow)
        self.dea = _EmaState(signal)
        self.seed: list[float] = []

    def push(self, c: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
        a, b = self.ef.push(c), self.es.push(c)
        if a is None or b is None:
            return None, None, None
        dif = a - b
        # 与批量 macd() 一致：DEA 的种子是**前 signal 个 dif 的均值**，不是 SMA-of-dif 之外的其它口径
        if self.dea.val is None:
            self.seed.append(dif)
            if len(self.seed) < self.signal:
                return dif, None, None
            self.dea.val = sum(self.seed) / self.signal
            self.seed.clear()
            dea = self.dea.val
        else:
            dea = self.dea.push(dif)
        return dif, dea, (None if dea is None else dif - dea)


class _BollState:
    """与批量 ``boll()`` 逐位一致：middle 走**累加式** SMA（同 ``sma()``），
    sd 用窗口内重新求均值的总体标准差（同 ``stdev()``）。两者末位会略有差异，
    这是原实现的既有行为，必须照抄才能 bit 相等。"""

    __slots__ = ("period", "k", "buf", "run")

    def __init__(self, period: int, k: float) -> None:
        self.period, self.k = period, k
        self.buf: list[float] = []
        self.run = 0.0

    def push(self, c: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
        if len(self.buf) < self.period:
            self.buf.append(c)
            if len(self.buf) < self.period:
                return None, None, None
            # 种子：内建 sum()（3.12 起补偿求和），与批量 sma() 完全一致
            self.run = sum(self.buf)
        else:
            old = self.buf.pop(0)
            self.buf.append(c)
            # 与批量 sma() 的 `run += values[i] - values[i - period]` 同序，保证逐位相等
            self.run += c - old
        mid = self.run / self.period
        m = sum(self.buf) / self.period
        sd = (sum((x - m) ** 2 for x in self.buf) / self.period) ** 0.5
        return mid, mid + self.k * sd, mid - self.k * sd


class StreamState:
    """逐根喂入的增量状态机；输出与批量函数逐位相等。

    specs 形如 ``[{"kind": "ema", "params": [20, 50, 200], "columns": ["ema20", ...]}, ...]``
    （由 ``indicator_config.parse_indicators`` 产出）。
    """

    def __init__(self, specs: Iterable[dict]) -> None:
        self._specs = list(specs)
        self._impl: list[list] = []
        for spec in self._specs:
            kind, params = spec["kind"], tuple(spec["params"])
            built: list = []
            if kind in ("ema", "ma"):
                built = [_EmaState(p) for p in params]
            elif kind == "sma":
                built = [_SmaState(p) for p in params]
            elif kind == "atr":
                built = [_AtrState(p) for p in params]
            elif kind == "rsi":
                built = [_RsiState(p) for p in params]
            elif kind == "macd":
                built = [_MacdState(*params)]
            elif kind == "boll":
                built = [_BollState(params[0], params[1] if len(params) > 1 else 2.0)]
            else:  # pragma: no cover - 由 config 层拦截
                raise ValueError(f"unsupported indicator kind: {kind!r}")
            self._impl.append(built)

    def push(self, o: float, h: float, l: float, c: float) -> dict[str, Optional[float]]:
        out: dict[str, Optional[float]] = {}
        for spec, impl in zip(self._specs, self._impl):
            kind, cols = spec["kind"], spec["columns"]
            if kind in ("ema", "ma", "sma"):
                for col, st in zip(cols, impl):
                    out[col] = st.push(c)
            elif kind == "atr":
                for col, st in zip(cols, impl):
                    out[col] = st.push(h, l, c)
            elif kind == "rsi":
                for col, st in zip(cols, impl):
                    out[col] = st.push(c)
            elif kind == "macd":
                dif, dea, hist = impl[0].push(c)
                out[cols[0]], out[cols[1]], out[cols[2]] = dif, dea, hist
            elif kind == "boll":
                mid, up, low = impl[0].push(c)
                out[cols[0]], out[cols[1]], out[cols[2]] = mid, up, low
        return out
