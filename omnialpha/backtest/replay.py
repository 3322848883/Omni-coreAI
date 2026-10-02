"""journal 回放回测：读 memory_journal.jsonl 逐 bar 模拟 → PnL/绩效。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .stats import buy_and_hold_pnl, deflated_sharpe, sma_crossover_pnl, t_test_pvalue


class BacktestReplayer:
    """从 journal 回放策略决策，模拟持仓/PnL。"""

    def __init__(self, root: Path, bot_id: str):
        self.root = Path(root)
        self.bot_id = bot_id
        self.journal_path = self.root / "data" / "bots" / bot_id / "state" / "memory_journal.jsonl"

    def replay(self, days: int = 30, closes: Optional[list[float]] = None) -> dict:
        """回放 journal → 模拟绩效。

        closes: 历史收盘价序列（回测区间），不传则只统计 journal 内数据。
        """
        entries = self._read_journal(days)
        if not entries:
            return {"error": "no journal entries", "n_cycles": 0}

        trades = self._simulate_trades(entries, closes or [])
        pnl_curve = self._build_pnl_curve(trades)
        returns = self._to_returns(pnl_curve)

        n_trials = max(1, len(entries))  # 每个 cycle 视为一次 trial
        metrics = {
            "period_days": days,
            "n_cycles": len(entries),
            "n_trades": len(trades),
            "total_pnl_usd": round(sum(t["pnl"] for t in trades), 2),
            "win_rate": round(sum(1 for t in trades if t["pnl"] > 0) / max(len(trades), 1), 3),
            "sharpe": round(_sharpe(returns), 3),
            "max_dd_pct": round(self._max_drawdown(pnl_curve), 3),
            "dsr": round(deflated_sharpe(returns, n_trials), 3),
            "p_value": round(t_test_pvalue(returns), 4),
        }
        if closes:
            metrics["benchmark_bh_pct"] = round(buy_and_hold_pnl(closes), 2)
            metrics["benchmark_sma_pct"] = round(sma_crossover_pnl(closes), 2)
        return metrics

    def _read_journal(self, days: int) -> list[dict]:
        if not self.journal_path.exists():
            return []
        import time
        cutoff = int(time.time()) - days * 86400
        entries = []
        for line in self.journal_path.read_text(encoding="utf-8").strip().splitlines():
            try:
                rec = json.loads(line)
                if rec.get("ts", 0) >= cutoff:
                    entries.append(rec)
            except Exception:  # noqa: BLE001
                continue
        return entries

    def _simulate_trades(self, entries: list[dict], closes: list[float]) -> list[dict]:
        """从 journal 决策模拟 trades（简化：hold→PnL=0，executed→按决策方向估算）。"""
        trades = []
        for e in entries:
            if not e.get("executed"):
                continue
            decision = e.get("decision", "")
            exec_r = e.get("exec_result") or {}
            pnl = float(exec_r.get("pnl_usd", 0))
            trades.append({
                "cycle_id": e.get("cycle_id"),
                "decision": decision,
                "pnl": pnl,
                "ts": e.get("ts"),
            })
        return trades

    def _build_pnl_curve(self, trades: list[dict]) -> list[float]:
        curve = [0.0]
        for t in trades:
            curve.append(curve[-1] + t["pnl"])
        return curve

    def _to_returns(self, curve: list[float]) -> list[float]:
        if len(curve) < 2:
            return [0.0]
        return [curve[i] - curve[i - 1] for i in range(1, len(curve))]

    def _max_drawdown(self, curve: list[float]) -> float:
        peak = float("-inf")
        max_dd = 0.0
        for v in curve:
            peak = max(peak, v)
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd


def _sharpe(returns: list[float], rf: float = 0) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = var ** 0.5
    return ((mean - rf) / std * (n ** 0.5)) if std > 0 else 0.0
