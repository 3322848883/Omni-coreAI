"""策略画像：data/bots/<bot>/state/memory_profile.json（确定性聚合，不走 LLM）。

平仓后更新统计。精简后注入 system prompt（~50t）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional


class MemoryProfile:
    """策略历史表现画像（跨订单持久）。"""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root: Path, bot_id: str):
        self.root = Path(root)
        self.bot_id = bot_id
        self.path = self.root / "data" / "bots" / bot_id / "state" / "memory_profile.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path)
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            self._lock = self._locks[key]

    def ledger_stats(self) -> Optional[dict]:
        """从 paper 账本汇总**核心统计**（权威口径）；无账本返回 None。

        **为什么画像要以账本为准**：文件式累加（`record_trade`）只能记「被检测到的
        平仓事件」，而

        - 单 bot 路径（`loop.py::run_once`）**根本没有平仓钩子** → 整个 `*-paper`
          舰队的画像一直是空的
        - SL/TP 触发是交易所侧成交、**没有信号** → 永远检测不到

        实测：459 笔真实平仓，文件式累加只记了 1 笔（458 笔被漏掉，且漏掉的恰好
        包含止损那些负面样本 → 胜率被系统性抬高）。

        改为**读时投影**：覆盖全部平仓、全部 bot，而且幂等 —— 不依赖任何触发点，
        也就不会再出现「某个路径忘了接线」。`by_action`/`avg_hold_rounds` 账本
        给不出来，仍由 `record_trade` 尽力而为。
        """
        from ..paper.store import realized_pnl_stats
        st = realized_pnl_stats(
            self.root / "data" / "bots" / self.bot_id / "paper" / "account.db")
        if not st:
            return None
        return {
            "total_trades": st["trades"],
            "win_count": st["wins"],
            "total_pnl_usd": st["pnl"],
            "max_drawdown_usd": st["worst"],
        }

    def load(self) -> dict:
        if not self.path.exists():
            rec = {
                "total_trades": 0, "win_count": 0, "win_rate": 0.0,
                "total_pnl_usd": 0.0, "avg_pnl_usd": 0.0,
                "avg_hold_rounds": 0, "max_drawdown_usd": 0.0,
                "best_act": "", "worst_act": "", "by_action": {},
                "updated_at": 0,
            }
        else:
            try:
                rec = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                rec = {"total_trades": 0, "win_count": 0, "total_pnl_usd": 0.0}
        # 账本权威：有 paper 账本时核心计数以它为准（无账本则沿用文件里的累加值）
        stats = self.ledger_stats()
        if stats:
            rec.update(stats)
        # 派生字段：老文件里没有，读的时候补上，保证调用方永远拿得到（设计 S2.5）
        return self._derive(rec)

    @staticmethod
    def _derive(rec: dict) -> dict:
        """由计数派生 `win_rate` / `avg_pnl_usd` / `best_act` / `worst_act`。

        **派生而不是单独累加**：这样老画像文件（只有 win_count/total_pnl_usd）
        读出来也立刻带上新字段，不必迁移。
        """
        n = int(rec.get("total_trades") or 0)
        rec["win_rate"] = round(int(rec.get("win_count") or 0) / n, 3) if n else 0.0
        rec["avg_pnl_usd"] = round(float(rec.get("total_pnl_usd") or 0.0) / n, 2) if n else 0.0
        by = rec.get("by_action") or {}
        ranked = [(a, (v.get("pnl") or 0.0) / max(int(v.get("n") or 0), 1))
                  for a, v in by.items() if int(v.get("n") or 0) > 0]
        if ranked:
            ranked.sort(key=lambda x: -x[1])
            rec["best_act"] = ranked[0][0]
            rec["worst_act"] = ranked[-1][0] if len(ranked) > 1 else ""
        else:
            rec.setdefault("best_act", "")
            rec.setdefault("worst_act", "")
        return rec

    def record_trade(self, *, pnl_usd: float, hold_rounds: int = 0,
                     entry_price: float = 0, exit_price: float = 0,
                     action: str = "") -> dict:
        """平仓后记录一笔交易（纯统计，不走 LLM）。

        `action` 用于按动作归类（`by_action` → `best_act`/`worst_act`）。
        不传就不归类，其余统计照常。
        """
        with self._lock:
            rec = self.load()
            rec["total_trades"] = rec.get("total_trades", 0) + 1
            if pnl_usd > 0:
                rec["win_count"] = rec.get("win_count", 0) + 1
            rec["total_pnl_usd"] = round(rec.get("total_pnl_usd", 0.0) + pnl_usd, 2)
            if pnl_usd < 0 and abs(pnl_usd) > abs(rec.get("max_drawdown_usd", 0.0)):
                rec["max_drawdown_usd"] = round(pnl_usd, 2)
            n = rec["total_trades"]
            prev_avg = rec.get("avg_hold_rounds", 0.0)
            rec["avg_hold_rounds"] = round((prev_avg * (n - 1) + hold_rounds) / n, 1)
            act = str(action or "").strip()
            if act:
                by = dict(rec.get("by_action") or {})
                slot = dict(by.get(act) or {"n": 0, "pnl": 0.0})
                slot["n"] = int(slot.get("n") or 0) + 1
                slot["pnl"] = round(float(slot.get("pnl") or 0.0) + pnl_usd, 2)
                by[act] = slot
                rec["by_action"] = by
            rec["updated_at"] = int(time.time())
            self._write(self._derive(rec))
            return self._derive(rec)

    def prompt_summary(self) -> str:
        """精简后进 system prompt（~50t）。"""
        rec = self.load()
        if rec.get("total_trades", 0) == 0:
            return ""
        n = rec["total_trades"]
        wr = round(rec.get("win_rate", 0.0) * 100)
        avg_pnl = rec.get("avg_pnl_usd", 0.0)
        return (f"历史表现: {n}笔交易, 胜率{wr}%, "
                f"均持仓{rec.get('avg_hold_rounds', 0)}轮, 均盈亏{avg_pnl}u")

    def _write(self, rec: dict) -> None:
        self.path.write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
