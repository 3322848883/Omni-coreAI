"""策略画像：data/bots/<bot>/state/memory_profile.json（确定性聚合，不走 LLM）。

平仓后更新统计。精简后注入 system prompt（~50t）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


class MemoryProfile:
    """策略历史表现画像（跨订单持久）。"""

    def __init__(self, root: Path, bot_id: str):
        self.root = Path(root)
        self.bot_id = bot_id
        self.path = self.root / "data" / "bots" / bot_id / "state" / "memory_profile.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.path.exists():
            return {
                "total_trades": 0, "win_count": 0,
                "total_pnl_usd": 0.0, "avg_hold_rounds": 0,
                "max_drawdown_usd": 0.0, "updated_at": 0,
            }
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"total_trades": 0, "win_count": 0, "total_pnl_usd": 0.0}

    def record_trade(self, *, pnl_usd: float, hold_rounds: int = 0,
                     entry_price: float = 0, exit_price: float = 0) -> dict:
        """平仓后记录一笔交易（纯统计，不走 LLM）。"""
        rec = self.load()
        rec["total_trades"] = rec.get("total_trades", 0) + 1
        if pnl_usd > 0:
            rec["win_count"] = rec.get("win_count", 0) + 1
        rec["total_pnl_usd"] = round(rec.get("total_pnl_usd", 0.0) + pnl_usd, 2)
        if pnl_usd < 0 and abs(pnl_usd) > abs(rec.get("max_drawdown_usd", 0.0)):
            rec["max_drawdown_usd"] = round(pnl_usd, 2)
        # 滚动均值
        n = rec["total_trades"]
        prev_avg = rec.get("avg_hold_rounds", 0.0)
        rec["avg_hold_rounds"] = round((prev_avg * (n - 1) + hold_rounds) / n, 1)
        rec["updated_at"] = int(time.time())
        self._write(rec)
        return rec

    def prompt_summary(self) -> str:
        """精简后进 system prompt（~50t）。"""
        rec = self.load()
        if rec.get("total_trades", 0) == 0:
            return ""
        n = rec["total_trades"]
        wr = round(rec.get("win_count", 0) / n * 100) if n else 0
        avg_pnl = round(rec.get("total_pnl_usd", 0) / n, 1) if n else 0
        return (f"历史表现: {n}笔交易, 胜率{wr}%, "
                f"均持仓{rec.get('avg_hold_rounds', 0)}轮, 均盈亏{avg_pnl}u")

    def _write(self, rec: dict) -> None:
        self.path.write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
