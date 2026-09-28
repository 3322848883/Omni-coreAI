"""策略衰减检测：滚动窗口指标 + 阈值告警。

数据源：memory_journal.jsonl 的 decision/executed/exec_result。
输出：data/bots/<id>/state/perf_metrics.jsonl（append-only）。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Optional


class DecayDetector:
    """滚动窗口策略衰减检测。"""

    def __init__(self, root: Path, bot_id: str, window: int = 20):
        self.root = Path(root)
        self.bot_id = bot_id
        self.window = max(5, window)
        self.path = self.root / "data" / "bots" / bot_id / "state" / "perf_metrics.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._history: list[float] = []  # pnl 序列

    def record_cycle(self, cycle_id: str, decision: str,
                     executed: bool, pnl_usd: float = 0.0) -> dict:
        """每轮追加指标，返回当前滚动指标。"""
        self._history.append(pnl_usd)
        metrics = self._compute()
        rec = {
            "ts": int(time.time()),
            "cycle_id": cycle_id,
            "decision": decision,
            "executed": executed,
            "pnl_usd": pnl_usd,
            **metrics,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def check(self) -> Optional[dict]:
        """返回告警 dict 或 None。"""
        if len(self._history) < self.window:
            return None
        metrics = self._compute()
        hist = self._historical()
        alerts = []
        # 滚动 Sharpe 跌破历史一半（或历史为正但滚动为负）
        hist_sharpe = hist.get("sharpe", 0)
        roll_sharpe = metrics.get("rolling_sharpe", 0)
        if hist_sharpe > 0 and roll_sharpe < 0.5 * hist_sharpe:
            alerts.append(f"decay: rolling_sharpe {roll_sharpe:.2f} < 0.5*historical {hist_sharpe:.2f}")
        elif hist_sharpe > 0 and roll_sharpe < 0:
            alerts.append(f"decay: rolling_sharpe {roll_sharpe:.2f} turned negative (was {hist_sharpe:.2f})")
        # 胜率过低
        if metrics.get("n_trades", 0) >= 5 and metrics.get("win_rate", 1) < 0.3:
            alerts.append(f"decay: win_rate {metrics['win_rate']:.2f} < 0.3")
        if alerts:
            return {"ts": int(time.time()), "alerts": alerts, "metrics": metrics}
        return None

    def _compute(self) -> dict:
        recent = self._history[-self.window:]
        n = len(recent)
        wins = sum(1 for p in recent if p > 0)
        trades = sum(1 for p in recent if p != 0)
        mean = sum(recent) / n if n else 0
        var = sum((p - mean) ** 2 for p in recent) / max(n - 1, 1)
        std = math.sqrt(var) if var > 0 else 0
        sharpe = (mean / std * math.sqrt(self.window)) if std > 0 else 0
        return {
            "rolling_sharpe": round(sharpe, 3),
            "win_rate": round(wins / trades, 3) if trades else 0,
            "n_trades": trades,
            "rolling_pnl": round(sum(recent), 2),
        }

    def _historical(self) -> dict:
        n = len(self._history)
        if n < 2:
            return {}
        mean = sum(self._history) / n
        var = sum((p - mean) ** 2 for p in self._history) / max(n - 1, 1)
        std = math.sqrt(var) if var > 0 else 0
        sharpe = (mean / std * math.sqrt(n)) if std > 0 else 0
        return {"sharpe": round(sharpe, 3), "n": n}
