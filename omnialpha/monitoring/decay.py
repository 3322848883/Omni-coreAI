"""策略衰减检测：滚动窗口指标 + 阈值告警。

数据源：memory_journal.jsonl 的 decision/executed/exec_result。
输出：data/bots/<id>/state/perf_metrics.jsonl（append-only）。
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Optional


class DecayDetector:
    """滚动窗口策略衰减检测。"""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root: Path, bot_id: str, window: int = 20):
        self.root = Path(root)
        self.bot_id = bot_id
        self.window = max(5, window)
        self.path = self.root / "data" / "bots" / bot_id / "state" / "perf_metrics.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._history: list[float] = []  # pnl 序列
        # 与 `_history` **平行**：多币下同一个 bot 里不同币可能各自失效，账户级合并
        # 指标会说「在失血」却指不出是谁（T13）。长度必须与 `_history` 一致。
        self._symbols: list[str] = []
        self._last_equity: Optional[float] = None
        key = str(self.path)
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            self._lock = self._locks[key]
        self._load()

    def _load(self) -> None:
        """从 perf_metrics.jsonl 恢复滚动窗口与上次权益。

        **必须跨实例恢复**：`PlanRunner._record_decay()` 每轮都 new 一个 DecayDetector，
        实例内累积永远只有 1 条，而 `check()` 要求 `len(self._history) >= window`(默认 20)
        —— 于是 `check()` 永远返回 None、衰减告警永远不可能触发（与 HealthMonitor 当初
        `error_streak` 实例内计数、每轮新建实例而失效是同一类问题）。
        """
        try:
            if not self.path.is_file():
                return
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-500:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if "pnl_usd" in rec:
                    try:
                        self._history.append(float(rec.get("pnl_usd") or 0.0))
                        self._symbols.append(str(rec.get("symbol") or "").strip().upper())
                    except (TypeError, ValueError):
                        pass
                if rec.get("equity") is not None:
                    try:
                        self._last_equity = float(rec["equity"])
                    except (TypeError, ValueError):
                        pass
        except Exception:  # noqa: BLE001
            pass

    def record_cycle(self, cycle_id: str, decision: str,
                     executed: bool, pnl_usd: Optional[float] = None,
                     equity: Optional[float] = None, symbol: str = "") -> dict:
        """每轮追加指标，返回当前滚动指标。

        `pnl_usd` 缺省时用**权益差**推算（`equity − 上次 equity`）：plan-loop 不执行
        订单、拿不到已实现盈亏，而权益差是它手边可得、且能反映策略是否在失血的代理。
        注意它含未实现盈亏 / 手续费 / 资金费，是**近似**而非精确已实现盈亏。
        """
        with self._lock:
            if pnl_usd is None:
                if equity is not None and self._last_equity is not None:
                    pnl_usd = float(equity) - float(self._last_equity)
                else:
                    pnl_usd = 0.0
            if equity is not None:
                self._last_equity = float(equity)
            pnl_usd = float(pnl_usd)
            self._history.append(pnl_usd)
            self._symbols.append(str(symbol or "").strip().upper())
            metrics = self._compute()
            rec = {
                "ts": int(time.time()),
                "cycle_id": cycle_id,
                "decision": decision,
                "executed": executed,
                "pnl_usd": pnl_usd,
                **metrics,
            }
            if symbol:
                rec["symbol"] = str(symbol).strip().upper()
            if equity is not None:
                rec["equity"] = float(equity)
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
        #
        # **样本不足时不判**：窗口内没有盈亏变动（`n_trades == 0`，即全是 hold）时
        # `_compute()` 的 `std == 0` → `rolling_sharpe` 恒为 0，而 `0 < 0.5×正数`
        # 永远成立 → **每轮都报**。实盘佐证（2026-10-07，10 小时）：440 条 decay 告警
        # 占 1612 轮的 27%，其中 `n_trades: 0` 出现 381 次。这不是衰减，是没成交。
        # 同一个 `check()` 里 `win_rate` 那条早有 `n_trades >= 5` 的保护，这条漏了。
        hist_sharpe = hist.get("sharpe", 0)
        roll_sharpe = metrics.get("rolling_sharpe", 0)
        enough = metrics.get("n_trades", 0) >= 5
        if enough and hist_sharpe > 0 and roll_sharpe < 0.5 * hist_sharpe:
            alerts.append(f"decay: rolling_sharpe {roll_sharpe:.2f} < 0.5*historical {hist_sharpe:.2f}")
        elif enough and hist_sharpe > 0 and roll_sharpe < 0:
            alerts.append(f"decay: rolling_sharpe {roll_sharpe:.2f} turned negative (was {hist_sharpe:.2f})")
        # 胜率过低
        if metrics.get("n_trades", 0) >= 5 and metrics.get("win_rate", 1) < 0.3:
            alerts.append(f"decay: win_rate {metrics['win_rate']:.2f} < 0.3")
        # 按币：多币下账户级告警指不出是哪个币在失血 → 单独报（单币时这一层不存在，
        # 文案与改动前逐字相同）
        for s, m in (metrics.get("by_symbol") or {}).items():
            if m["n_trades"] >= 5 and m["win_rate"] < 0.3:
                alerts.append(f"decay [{s}]: win_rate {m['win_rate']:.2f} < 0.3")
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
        out = {
            "rolling_sharpe": round(sharpe, 3),
            "win_rate": round(wins / trades, 3) if trades else 0,
            "n_trades": trades,
            "rolling_pnl": round(sum(recent), 2),
        }
        # 按币（账户级合并指标指不出是谁在失血）。`_symbols` 与 `_history` 平行；
        # 老记录没有 symbol → 空串，跳过。**只有一个币时不加这一层** ——
        # 单币的状态文件与告警文案保持逐字不变（I11）。
        syms = self._symbols[-self.window:]
        if len(syms) == len(recent):
            groups: dict[str, list] = {}
            for p, s in zip(recent, syms):
                if s:
                    groups.setdefault(s, []).append(p)
            if len(groups) > 1:
                out["by_symbol"] = {
                    s: {"rolling_pnl": round(sum(ps), 2),
                        "win_rate": round(sum(1 for p in ps if p > 0)
                                          / max(sum(1 for p in ps if p != 0), 1), 3),
                        "n_trades": sum(1 for p in ps if p != 0)}
                    for s, ps in sorted(groups.items())
                }
        return out

    def _historical(self) -> dict:
        n = len(self._history)
        if n < 2:
            return {}
        mean = sum(self._history) / n
        var = sum((p - mean) ** 2 for p in self._history) / max(n - 1, 1)
        std = math.sqrt(var) if var > 0 else 0
        sharpe = (mean / std * math.sqrt(n)) if std > 0 else 0
        return {"sharpe": round(sharpe, 3), "n": n}
