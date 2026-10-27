"""指标配置层：`watchlist.yaml` 的 `indicators:` → 规范 spec / 列名 / 预热根数 / 哈希。

配置写法（与 Pine 风格一致）：

```yaml
indicators:
  - ema: [20, 50, 200]
  - atr: [14]
  - rsi: [14]
  - macd: [12, 26, 9]
  - boll: [20, 2]
```

设计要点
- **列名 = 指标名**，与 ``omnialpha/strategist/indicators.py:parse_indicator_name`` 对齐，
  读取端可直接 ``row["ema200"]``；
- 只有配置里出现的才会产生列（不配置不加列、不计算）；
- 预热根数用于「新列回填起点」与「max_candles 下限校验」，口径与读取端
  ``indicator_warmup_bars`` 一致（长周期 ×5 + 5，短周期 +5，MACD 另加 signal）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "IndicatorSpec",
    "DEFAULT_INDICATORS",
    "SUPPORTED_KINDS",
    "parse_indicators",
    "indicator_columns",
    "warmup_bars",
    "spec_hash",
]

SUPPORTED_KINDS = ("ema", "ma", "sma", "atr", "rsi", "macd", "boll")

#: 默认落库集合：9 个声明 → 11 个物理列
DEFAULT_INDICATORS: list[dict[str, list[Any]]] = [
    {"ema": [20, 50, 200]},
    {"atr": [14]},
    {"rsi": [14]},
    {"macd": [12, 26, 9]},
    {"boll": [20, 2]},
]

_LONG_PERIOD = 50
_WARMUP_MULT = 5
_WARMUP_PAD = 5


@dataclass(frozen=True)
class IndicatorSpec:
    kind: str
    params: tuple
    columns: tuple[str, ...]
    warmup: int

    def as_dict(self) -> dict:
        return {"kind": self.kind, "params": list(self.params),
                "columns": list(self.columns), "warmup": self.warmup}


def _fmt_num(v: float) -> str:
    """2.5 → '2p5'（列名里不能有小数点）。"""
    s = f"{float(v):g}"
    return s.replace(".", "p").replace("-", "n")


def _columns(kind: str, params: tuple) -> tuple[str, ...]:
    if kind in ("ema", "ma", "sma"):
        return tuple(f"{kind}{int(p)}" for p in params)
    if kind in ("atr", "rsi"):
        return tuple(f"{kind}{int(p)}" for p in params)
    if kind == "macd":
        fast, slow, signal = (int(x) for x in params)
        if (fast, slow, signal) == (12, 26, 9):
            return ("macd", "macd_dea", "macd_hist")
        tag = f"{fast}_{slow}_{signal}"
        return (f"macd_{tag}", f"macd_{tag}_dea", f"macd_{tag}_hist")
    if kind == "boll":
        period = int(params[0])
        k = float(params[1]) if len(params) > 1 else 2.0
        if k == 2.0:
            return (f"boll{period}", f"boll{period}_upper", f"boll{period}_lower")
        tag = f"boll{period}_{_fmt_num(k)}"
        return (tag, f"{tag}_upper", f"{tag}_lower")
    raise ValueError(f"unsupported indicator kind: {kind!r}")


def _warmup(kind: str, params: tuple) -> int:
    """预热根数取该 spec 内**最长周期**（`ema:[20,200]` 必须按 200 算）。"""
    if kind == "macd":
        fast, slow, signal = (int(x) for x in params)
        base = slow * _WARMUP_MULT if slow >= _LONG_PERIOD else slow
        return base + signal + _WARMUP_PAD
    if kind == "boll":
        period = int(params[0])
        return (period * _WARMUP_MULT if period >= _LONG_PERIOD else period) + _WARMUP_PAD
    period = max(int(p) for p in params)
    return (period * _WARMUP_MULT if period >= _LONG_PERIOD else period) + _WARMUP_PAD


def _normalize(raw: Any) -> list[tuple[str, tuple]]:
    """把 yaml 的混合形态统一成 [(kind, params)]，保序去重。"""
    if raw is None:
        raw = DEFAULT_INDICATORS
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"indicators must be a list, got {type(raw).__name__}")
    out: list[tuple[str, tuple]] = []
    seen: set[tuple[str, tuple]] = set()
    for item in raw:
        if isinstance(item, str):
            pairs = [(item.strip().lower(), ())]
        elif isinstance(item, dict):
            pairs = []
            for k, v in item.items():
                kind = str(k).strip().lower()
                if isinstance(v, (list, tuple)):
                    params = tuple(v)
                elif v is None:
                    params = ()
                else:
                    params = (v,)
                pairs.append((kind, params))
        else:
            raise ValueError(f"unsupported indicators entry: {item!r}")
        for kind, params in pairs:
            if kind not in SUPPORTED_KINDS:
                raise ValueError(f"unsupported indicator kind: {kind!r} "
                                 f"(supported: {', '.join(SUPPORTED_KINDS)})")
            if kind == "macd":
                params = params or (12, 26, 9)
                if len(params) != 3:
                    raise ValueError(f"macd needs [fast, slow, signal], got {list(params)}")
            elif kind == "boll":
                params = params or (20, 2)
                if len(params) not in (1, 2):
                    raise ValueError(f"boll needs [period] or [period, k], got {list(params)}")
                if len(params) == 1:
                    params = (params[0], 2.0)
            else:
                if not params:
                    raise ValueError(f"{kind} needs at least one period, e.g. {kind}: [14]")
                if any(int(p) < 2 for p in params):
                    raise ValueError(f"{kind} period must be >= 2, got {list(params)}")
                # 周期列表与书写顺序无关：归一成升序，列名与指纹随之稳定
                params = tuple(sorted(int(p) for p in params))
            key = (kind, tuple(params))
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def parse_indicators(raw: Any = None) -> list[IndicatorSpec]:
    specs: list[IndicatorSpec] = []
    for kind, params in _normalize(raw):
        cols = _columns(kind, params)
        specs.append(IndicatorSpec(kind=kind, params=params, columns=cols,
                                   warmup=_warmup(kind, params)))
    return specs


def indicator_columns(specs: Iterable[IndicatorSpec]) -> list[str]:
    cols: list[str] = []
    for s in specs:
        cols.extend(s.columns)
    return cols


def warmup_bars(specs: Iterable[IndicatorSpec]) -> int:
    return max((s.warmup for s in specs), default=0)


def spec_hash(specs: Iterable[IndicatorSpec]) -> str:
    """与键序无关的稳定指纹，写入 kline_meta.indicator_spec_hash。"""
    payload = sorted(
        json.dumps({"kind": s.kind, "params": list(s.params)}, sort_keys=True,
                   separators=(",", ":"))
        for s in specs
    )
    return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()[:16]
