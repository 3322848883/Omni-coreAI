"""TV Indicator 7: Cumulative Delta Volume (LonesomeTheBlue)

CDV = 逐根 delta 的累积。delta 由 **K 线几何**估算 —— 在没有逐笔方向数据时，
这是业界通行的近似做法。

原版：`study("Cumulative Delta Volume", "CDV")`

核心是 `_rate()`：

    tw   = high - max(open, close)     上影线
    bw   = min(open, close) - low      下影线
    body = |close - open|              实体
    rate(cond) = 0.5 * (tw + bw + (cond ? 2*body : 0)) / (tw + bw + body)

    阳线（含平盘）: rate ∈ [0.5, 1]   实体越饱满越接近 1
    阴线:          rate ∈ [0, 0.5]   实体越饱满越接近 0

    deltaup   = volume * rate(open <= close)
    deltadown = volume * rate(open >  close)
    delta     = close >= open ? deltaup : -deltadown
    cumdelta  = cum(delta)

**实际生效的只有 cond 为真的那一支**：阳线走 `rate(o <= c)`、阴线走 `rate(o > c)`，
两者都是 `rate(True)`，即

    rate = 0.5 * (1 + body / (tw + bw + body))     ∈ [0.5, 1]

「实体占整根的比例」映射到 [0.5, 1]：纯 doji → 0.5，纯实体 → 1.0。
`rate(False)` 那一支（值域 [0, 0.5]）在原版里**从不被读取**——它只出现在
`deltaup`/`deltadown` 的另一个分支上，而那个分支不会被 `delta` 取用。

两条与原版逐字对齐、**不要「优化」**的边界行为：

  1. `nz(ret) == 0 ? 0.5 : ret` —— 不仅 na 被替换，ret 恰好为 0 也替换成 0.5。
     在 `rate(True)` 支上不会触发（其最小值为 0.5），只在 `rate(False)` 支的
     纯实体情形触发。
  2. `tw + bw + body == 0`（h == l == o == c，完全无波动）→ 除零得 na → 0.5。

**这是估算，不是真 Delta。** 若数据源能给出交易所实测的主动买卖量
（Gate `/contract_stats` 的 `long_taker_size` / `short_taker_size`），那个更准，
见 `taker_delta` 工具。本模块只忠实移植原版算法，不做口径混用。
"""
from __future__ import annotations

from typing import Optional


def cdv_rate(high: float, low: float, o: float, c: float, cond: bool) -> float:
    """原版 `_rate(cond)`：实体占整根的比例映射到 [0,1] 区间上的买卖强度。"""
    tw = high - max(o, c)
    bw = min(o, c) - low
    body = abs(c - o)
    denom = tw + bw + body
    if denom == 0:
        return 0.5                      # 原版 0/0 = na → nz → 0.5
    ret = 0.5 * (tw + bw + (2.0 * body if cond else 0.0)) / denom
    if ret == 0:
        return 0.5                      # 原版 `nz(ret) == 0 ? 0.5 : ret`
    return ret


def cumulative_delta_volume(opens: list[float], highs: list[float],
                            lows: list[float], closes: list[float],
                            volumes: list[float]) -> dict:
    """逐根 delta 与累积 CDV。

    返回 `delta`（每根）与 `cdv`（累积），以及原版把 CDV 当价格画蜡烛时的
    OHLC 序列（`o = cumdelta[1]`，`h/l = 两根 CDV 的极值`）。
    """
    n = len(closes)
    deltas: list[float] = []
    cdv: list[float] = []
    acc = 0.0
    for i in range(n):
        o, h, l, c, v = opens[i], highs[i], lows[i], closes[i], volumes[i]
        r_up = cdv_rate(h, l, o, c, o <= c)
        r_dn = cdv_rate(h, l, o, c, o > c)
        d = (v * r_up) if c >= o else -(v * r_dn)
        deltas.append(d)
        acc += d
        cdv.append(acc)

    cdv_open: list[Optional[float]] = [None]
    cdv_high: list[Optional[float]] = [None]
    cdv_low: list[Optional[float]] = [None]
    for i in range(1, n):
        prev, cur = cdv[i - 1], cdv[i]
        cdv_open.append(prev)
        cdv_high.append(max(cur, prev))
        cdv_low.append(min(cur, prev))

    return {
        "delta": deltas,
        "cdv": cdv,
        "cdv_open": cdv_open,
        "cdv_high": cdv_high,
        "cdv_low": cdv_low,
        "cdv_close": list(cdv),
    }


def heikin_ashi_from(o: list[Optional[float]], h: list[Optional[float]],
                     l: list[Optional[float]], c: list[Optional[float]]) -> dict:
    """原版对 CDV 蜡烛套 Heikin-Ashi（首根 haopen = (o+c)/2）。"""
    n = len(c)
    ha_o: list[Optional[float]] = [None] * n
    ha_c: list[Optional[float]] = [None] * n
    ha_h: list[Optional[float]] = [None] * n
    ha_l: list[Optional[float]] = [None] * n
    for i in range(n):
        if o[i] is None or c[i] is None or h[i] is None or l[i] is None:
            continue
        ha_c[i] = (o[i] + h[i] + l[i] + c[i]) / 4.0
        if i == 0 or ha_o[i - 1] is None or ha_c[i - 1] is None:
            ha_o[i] = (o[i] + c[i]) / 2.0
        else:
            ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
        ha_h[i] = max(h[i], ha_o[i], ha_c[i])
        ha_l[i] = min(l[i], ha_o[i], ha_c[i])
    return {"open": ha_o, "high": ha_h, "low": ha_l, "close": ha_c}
