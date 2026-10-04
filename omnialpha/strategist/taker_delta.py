"""真 Delta / CVD —— 用交易所**实测**的主动买卖量计算。

数据源：Gate `/contract_stats` 的
  - `long_taker_size`   主动买入量（taker buy）
  - `short_taker_size`  主动卖出量（taker sell）
  - 二者之比即 `lsr_taker`（实测 `long_taker_size / short_taker_size`，误差 0）

与 `tv_cdv` 的分工：
  - `tv_cdv`    —— K 线几何**估算** delta（零依赖、任意周期，但只是近似）
  - 本模块      —— 交易所**实测** taker 量（准确，但仅覆盖该所支持的周期）

两者不是替代关系：估算版可覆盖本地库没有的周期，实测版作为准确基准。

**为什么不用逐笔成交算**：Gate `/trades` 的字段只有
`contract, create_time, create_time_ms, id, price, size` —— 没有方向标记，
所以逐笔 Delta 无法直接算；`contract_stats` 的聚合 taker 量是唯一准确的替代。
"""
from __future__ import annotations

from typing import Optional


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def taker_delta_series(stats: list[dict]) -> dict:
    """从 `contract_stats` 记录序列算逐期 Delta 与累积 CVD。

    入参 `stats` 需按时间**升序**。缺失 taker 字段的记录不参与累积
    （`acc` 保持不变），对应位置在 `delta`/`cvd` 里为 None——这样 CVD 不会
    因为一条脏数据而整体错位。
    """
    delta: list[Optional[float]] = []
    cvd: list[Optional[float]] = []
    acc = 0.0
    for x in stats or []:
        if not isinstance(x, dict):
            delta.append(None)
            cvd.append(None)
            continue
        lt, st = x.get("long_taker_size"), x.get("short_taker_size")
        if lt is None or st is None:
            delta.append(None)
            cvd.append(None)
            continue
        try:
            d = float(lt) - float(st)
        except (TypeError, ValueError):
            delta.append(None)
            cvd.append(None)
            continue
        acc += d
        delta.append(d)
        cvd.append(acc)
    return {"delta": delta, "cvd": cvd, "bars": len(delta)}


def summarize(stats: list[dict], tail: int = 10) -> dict:
    """把 `contract_stats` 序列压成订单流关心的摘要。

    除 Delta/CVD 外，还带上大户持仓与持仓人数——这些字段 `/contract_stats`
    一直都有，只是此前没有工具暴露。
    """
    rows = [x for x in (stats or []) if isinstance(x, dict)]
    if not rows:
        return {"error": "no stats rows"}
    r = taker_delta_series(rows)
    delta, cvd = r["delta"], r["cvd"]
    last = rows[-1]

    return {
        "bars": r["bars"],
        "delta_last": None if not delta or delta[-1] is None else round(delta[-1], 2),
        "cvd_last": None if not cvd or cvd[-1] is None else round(cvd[-1], 2),
        "delta_tail": [None if v is None else round(v, 2) for v in delta[-tail:]],
        "cvd_tail": [None if v is None else round(v, 2) for v in cvd[-tail:]],
        "taker": {
            "long_taker_size": _f(last.get("long_taker_size")),
            "short_taker_size": _f(last.get("short_taker_size")),
            "lsr_taker": _f(last.get("lsr_taker")),
        },
        "positioning": {
            "open_interest": _f(last.get("open_interest")),
            "open_interest_usd": _f(last.get("open_interest_usd")),
            "lsr_account": _f(last.get("lsr_account")),
            "top_lsr_account": _f(last.get("top_lsr_account")),
            "top_lsr_size": _f(last.get("top_lsr_size")),
            "top_long_size": _f(last.get("top_long_size")),
            "top_short_size": _f(last.get("top_short_size")),
            "long_users": _f(last.get("long_users")),
            "short_users": _f(last.get("short_users")),
        },
        "funding": {"last_funding_rate": _f(last.get("last_funding_rate"))},
        "liquidation": {
            "long_liq_size": _f(last.get("long_liq_size")),
            "short_liq_size": _f(last.get("short_liq_size")),
        },
        "last_time": last.get("time"),
    }
