"""影子对拍：新引擎 vs 旧增量实现，只统计不写库（P1 影子模式的核心）。

用途：切换前先让 watcher 用**新引擎**把同一根 bar 的指标算一遍，与旧实现（现在写库的值）
逐根比较，累积统计 n / max_rel / p99 / 超阈计数；达到门禁再切写入路径。

门禁（方案 §P1）：24 小时内 相对误差 >1e-6 的 bar 占比 < 0.1%。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

__all__ = ["ShadowStats", "MISMATCH_TOL", "GATE_RATIO"]


MISMATCH_TOL = 1e-6
GATE_RATIO = 0.001  # 0.1%


def _rel(a: float, b: float) -> float:
    if a == b:
        return 0.0
    return abs(a - b) / max(abs(a), abs(b), 1e-12)


@dataclass
class ShadowStats:
    """累计统计（进程内常驻，定期落日志/健康端点）。"""

    tol: float = MISMATCH_TOL
    n: int = 0
    mismatches: int = 0
    max_rel: float = 0.0
    worst: Optional[tuple] = None
    by_column: dict = field(default_factory=dict)

    def record(self, column: str, new_value, old_value, *, t: Optional[int] = None) -> float:
        """比较一根 bar 的一个指标，返回本次相对误差。任一侧为 None 时跳过（不计入 n）。"""
        if new_value is None or old_value is None:
            return 0.0
        rel = _rel(float(new_value), float(old_value))
        self.n += 1
        bucket = self.by_column.setdefault(column, {"n": 0, "bad": 0, "max_rel": 0.0})
        bucket["n"] += 1
        if rel > bucket["max_rel"]:
            bucket["max_rel"] = rel
        if rel > self.max_rel:
            self.max_rel = rel
            self.worst = (t, column, new_value, old_value)
        if rel > self.tol:
            self.mismatches += 1
            bucket["bad"] += 1
        return rel

    def record_row(self, t: int, new: dict, old: dict, columns=None) -> None:
        cols = columns if columns is not None else [k for k in new if k in old]
        for c in cols:
            self.record(c, new.get(c), old.get(c), t=t)

    @property
    def ratio(self) -> float:
        return (self.mismatches / self.n) if self.n else 0.0

    def passed(self, gate: float = GATE_RATIO) -> bool:
        return self.n > 0 and self.ratio < gate

    def summary(self) -> dict:
        return {
            "n": self.n,
            "mismatches": self.mismatches,
            "ratio": round(self.ratio, 6),
            "max_rel": self.max_rel,
            "worst": self.worst,
            "passed": self.passed(),
            "by_column": self.by_column,
        }

    def reset(self) -> None:
        self.n = 0
        self.mismatches = 0
        self.max_rel = 0.0
        self.worst = None
        self.by_column.clear()
