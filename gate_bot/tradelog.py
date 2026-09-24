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
    ) -> dict[str, Any]:
        return self.write(
            {
                "type": "execution",
                "bot_id": bot_id,
                "source": source,
                "plan_cycle": (signal_meta or {}).get("plan_cycle") or (signal_meta or {}).get("signal_id"),
                "strategy": (signal_meta or {}).get("strategy"),
                "ok": report.get("ok"),
                "steps": report.get("steps"),
            }
        )

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
    return root / "logs" / "trades" / f"{bot_id}.jsonl"
