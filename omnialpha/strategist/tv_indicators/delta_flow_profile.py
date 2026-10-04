"""TV Indicator 4: Delta Flow Profile (LuxAlgo)

Money Flow Profile + Delta Profile + Level of Significance (PoC) + Developing PoC。

原版：`indicator("Delta Flow Profile [LuxAlgo]")`，纯 OHLCV，不依赖外部数据源。

与 `oi_visible_range` / `vol_oi_footprint` 的差别：本指标累加的是 **money flow**
（`volume × 档位重叠比例 × 档位中间价`）而非纯成交量，并逐 bar 输出 PoC 迁移轨迹。

移植约定（逐条对齐原版，不自行「优化」）：
  - 区间长度对齐 `rpLN := last_bar_index > rpLN ? rpLN - 1 : last_bar_index`：
    数据充足时区间**恰好 `lookback` 根**（不是 `lookback + 1`），不足时用全部
  - `vPOR` 四种情况的判定顺序与原版 if / else-if 一致——顺序影响边界归属
  - `bull` 判定：`Bar Polarity` 用 `c > o`（严格大于，doji 归空）
  - PoC 取**第一个**最大值（对齐 `array.indexof`），Developing PoC 逐 bar 记录
  - 档位是 `rows` 个区间（`[pLL, pLL+pSTP)` 半开），恰好等于最高价的 bar
    不计入最高档

两处与原版的**刻意差异**（原版在此会得到 na 并污染整列累计，故收敛）：
  - `bh == bl`（无波动 bar）时原版 `vPOR` 算出 0/0 = na，此处取 1.0
  - 输出是**数值摘要**；原版的绘图（box 剖面 / PoC polyline / 节点文字）未移植

`delta_norm` 的绝对值即原版 `DpM`（`|bbp| / max|bbp|`），其符号即原版配色依据
（`bbp > 0` 取买方色、否则取卖方色）——一个字段同时承载了原版的两项信息。
"""

from __future__ import annotations

from typing import Optional

POLARITY_BAR = "bar_polarity"        # up => close > open
POLARITY_PRESSURE = "bar_pressure"   # up => (close - low) > (high - close)


def bull_flags(opens: list[float], highs: list[float], lows: list[float],
               closes: list[float], polarity: str = POLARITY_BAR) -> list[bool]:
    """原版 `bull`：两种极性判定方式。"""
    if polarity == POLARITY_PRESSURE:
        return [(closes[i] - lows[i]) > (highs[i] - closes[i]) for i in range(len(closes))]
    return [closes[i] > opens[i] for i in range(len(closes))]


def overlap_ratio(bl: float, bh: float, pLL: float, pSTP: float) -> float:
    """原版 `vPOR`：bar `[bl, bh]` 落在档位 `[pLL, pLL+pSTP)` 内的比例。

    四种情况的判定顺序与原版 if / else-if 一致——顺序影响边界归属，
    换序会改变结果。`bh == bl`（无波动 bar）原版会算出 na，这里收敛为 1.0
    （该 bar 必然整根落在某个档位内）。
    """
    if bh == bl:
        return 1.0
    if bl >= pLL and bh > pLL + pSTP:          # 下端在档内、上端超出
        return (pLL + pSTP - bl) / (bh - bl)
    if bh <= pLL + pSTP and bl < pLL:          # 上端在档内、下端超出
        return (bh - pLL) / (bh - bl)
    if bl >= pLL and bh <= pLL + pSTP:         # bar 完全落在档内
        return 1.0
    return pSTP / (bh - bl)                    # bar 完全覆盖该档


def delta_flow_profile(opens: list[float], highs: list[float], lows: list[float],
                       closes: list[float], volumes: list[float],
                       lookback: int = 360, rows: int = 25,
                       polarity: str = POLARITY_BAR) -> dict:
    """计算 Delta Flow Profile。

    lookback 对齐原版 `rpLN`（默认 360），rows 对齐原版 `rpNR`（默认 25）。
    """
    n = len(closes)
    if n < 3:
        return {"error": "not enough bars"}

    # 原版 `rpLN := last_bar_index > rpLN ? rpLN - 1 : last_bar_index`
    last_index = n - 1
    rpln = (int(lookback) - 1) if last_index > int(lookback) else last_index
    span = rpln + 1
    if span < 2:
        return {"error": "lookback too small"}
    nr = max(1, int(rows))

    start = n - span
    pLST = min(lows[start:])
    pHST = max(highs[start:])
    if not pHST > pLST:
        return {"error": "flat price range (high == low)"}
    pSTP = (pHST - pLST) / nr

    bull = bull_flags(opens, highs, lows, closes, polarity)

    rpVST = [0.0] * nr     # 总 money flow（买 + 卖）
    rpVSB = [0.0] * nr     # 仅多头 bar 的 money flow
    poc_track: list[tuple[int, int, float]] = []

    for idx in range(start, n):
        bh = highs[idx]
        bl = lows[idx]
        v = volumes[idx]
        for l in range(nr):
            pLL = pLST + l * pSTP
            if bh >= pLL and bl < pLL + pSTP:
                vpor = overlap_ratio(bl, bh, pLL, pSTP)
                mf = v * vpor * (pLST + (l + 0.5) * pSTP)
                rpVST[l] += mf
                if bull[idx]:
                    rpVSB[l] += mf
        # Developing PoC：每根 bar 记一次累计 PoC（原版在 bar 循环内 push）
        li = rpVST.index(max(rpVST))
        poc_track.append((idx - start, li, pLST + (li + 0.5) * pSTP))

    vtMX = max(rpVST)
    delta_rows = [2.0 * rpVSB[l] - rpVST[l] for l in range(nr)]
    vdMX = max((abs(x) for x in delta_rows), default=0.0)

    total_mf = sum(rpVST)
    total_delta = 2.0 * sum(rpVSB) - total_mf

    poc_level = rpVST.index(vtMX)
    levels = []
    for l in range(nr):
        levels.append({
            "price": round(pLST + (l + 0.5) * pSTP, 6),
            "money_flow_norm": None if vtMX <= 0 else round(rpVST[l] / vtMX, 6),
            "delta_norm": None if vdMX <= 0 else round(delta_rows[l] / vdMX, 6),
        })

    # PoC 迁移：只保留档位发生变化的点（累积 PoC 早期会频繁跳，后期趋稳）
    changes = []
    last_li: Optional[int] = None
    for off, li, price in poc_track:
        if li != last_li:
            changes.append({"offset": off, "level": li, "price": round(price, 6)})
            last_li = li
    first = changes[0] if changes else None
    last = changes[-1] if changes else None
    if first and last:
        if last["level"] > first["level"]:
            direction = "up"
        elif last["level"] < first["level"]:
            direction = "down"
        else:
            direction = "flat"
    else:
        direction = "flat"

    return {
        "profile": {
            "low": round(pLST, 6), "high": round(pHST, 6),
            "rows": nr, "step": round(pSTP, 6), "bars": span,
        },
        "polarity": polarity,
        "total_money_flow": round(total_mf, 2),
        "total_delta": round(total_delta, 2),
        "delta_dominant": "buyers" if total_delta > 0 else "sellers",
        "poc": {
            "level": poc_level,
            "price": round(pLST + (poc_level + 0.5) * pSTP, 6),
            "money_flow": round(vtMX, 2),
            "share_pct": None if total_mf <= 0 else round(vtMX / total_mf * 100.0, 2),
        },
        "levels": levels,
        "poc_path": {
            "start_level": first["level"] if first else None,
            "start_price": first["price"] if first else None,
            "end_level": last["level"] if last else None,
            "end_price": last["price"] if last else None,
            "direction": direction,
            "changes_count": len(changes),
            "recent_changes": changes[-8:],
        },
    }
