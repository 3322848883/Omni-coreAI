"""Append-only trade journal (JSONL) for executed plans/orders."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class TradeLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _ledger_root(self) -> Optional[Path]:
        """从 trade_log_path() 的布局反推 root：`<root>/data/bots/<bot_id>/logs/trades.jsonl`。

        只在布局匹配时返回；不匹配返回 None。**宁可跳过 ledger 写入，也不要用
        「往上数几层」猜一个目录** —— 原来写死 `parents[2]` 少算一层，把 ledger
        写到了 `<root>/data/bots/data/bots.db`，6061 笔真实成交全部落错库
        （见 tests/test_tradelog_ledger_path.py）。
        """
        parts = self.path.resolve().parts
        # .../data/bots/<bot_id>/logs/trades.jsonl
        if len(parts) >= 5 and parts[-1] == "trades.jsonl" and parts[-2] == "logs" \
                and parts[-4] == "bots" and parts[-5] == "data":
            return Path(*parts[:-5])
        return None

    def write(self, event: dict[str, Any]) -> dict[str, Any]:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def log_execution(
        self,
        bot_id: str,
        signal_meta: dict,
        report: dict,
        source: str = "watcher",
        env: str = "live",
    ) -> dict[str, Any]:
        row = self.write(
            {
                "type": "execution",
                "bot_id": bot_id,
                "source": source,
                # `order_id`：人格组下单时写在 signal.meta 里。**必须落进成交日志** ——
                # 否则「这笔单的已实现盈亏」无法回连到人格组的订单记录（memory 画像要靠它）。
                # persona 信号没有 plan_cycle，所以不能拿 plan_cycle 当 join 键。
                "order_id": (signal_meta or {}).get("order_id"),
                "plan_cycle": (signal_meta or {}).get("plan_cycle") or (signal_meta or {}).get("signal_id"),
                "strategy": (signal_meta or {}).get("strategy"),
                "ok": report.get("ok"),
                "steps": report.get("steps"),
            }
        )
        # SQLite ledger (best-effort)
        try:
            from .ledger import Ledger, default_ledger_path

            root = self._ledger_root()
            if root is not None:
                led = Ledger(default_ledger_path(root))
                led.insert_trade(
                    bot_id,
                    plan_cycle=row.get("plan_cycle"),
                    action="execution",
                    ok=bool(report.get("ok")),
                    steps=report.get("steps"),
                    source=source,
                )
                led.close()
        except Exception:  # noqa: BLE001
            pass
        # 成交事件推送（开/平/减仓/改保护；无渠道或模拟盘关闭则静默）
        try:
            from .monitoring import notify_trade_events

            notify_trade_events(bot_id, report.get("steps"), env=env)
        except Exception:  # noqa: BLE001
            pass
        return row

    def log_plan(self, bot_id: str, plan_result: dict[str, Any]) -> dict[str, Any]:
        return self.write({"type": "plan", "bot_id": bot_id, **plan_result})

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        rows = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


def trade_log_path(root: Path, bot_id: str) -> Path:
    from .paths import bot_paths

    return bot_paths(root, bot_id, create=True).logs / "trades.jsonl"
