"""健康监控：延迟/错误/心跳告警。

数据文件：data/bots/<id>/state/health.json（每轮覆盖更新）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class HealthMonitor:
    """轻量健康监控。不做 Kafka/Flink，只写 JSON + 阈值告警。"""

    def __init__(self, root: Path, bot_id: str,
                 llm_latency_warn: float = 120.0,
                 exec_latency_warn: float = 30.0,
                 error_warn: int = 5,
                 heartbeat_crit: int = 600):
        self.root = Path(root)
        self.bot_id = bot_id
        self.path = self.root / "data" / "bots" / bot_id / "state" / "health.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.llm_latency_warn = llm_latency_warn
        self.exec_latency_warn = exec_latency_warn
        self.error_warn = error_warn
        self.heartbeat_crit = heartbeat_crit
        # 调用方普遍写成 HealthMonitor(root, bid).record_error() —— 每次都是新实例。
        # 不从盘上续读，连续失败数每轮都从 0 开始，等于没记（heartbeat 也就永远写 0）。
        self._error_streak = int(self._load().get("error_streak") or 0)

    def _load(self) -> dict:
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8"))
            return rec if isinstance(rec, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def heartbeat(self, **metrics: Any) -> dict:
        """每轮更新健康指标（整体覆盖：未传的指标视为无）。"""
        rec = {
            "ts": int(time.time()),
            "last_heartbeat": int(time.time()),
            "error_streak": self._error_streak,
            **metrics,
        }
        self.path.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return rec

    def _write_streak(self) -> None:
        """只更新连续失败数，保留 health.json 里其它指标。"""
        rec = self._load()
        rec.update({
            "ts": int(time.time()),
            "last_heartbeat": int(time.time()),
            "error_streak": self._error_streak,
        })
        self.path.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def record_error(self, detail: str = "") -> int:
        """记一次失败并落盘，返回当前连续失败次数。

        每 error_warn 次落一条 alerts.json（5/10/15…），避免逐轮刷屏。
        """
        self._error_streak += 1
        self._write_streak()
        if self._error_streak % self.error_warn == 0:
            self._raise_streak_alert(detail)
        return self._error_streak

    def record_success(self) -> None:
        if self._error_streak:
            self._error_streak = 0
            self._write_streak()

    def _raise_streak_alert(self, detail: str) -> None:
        try:
            from .alerts import TYPE_PLAN_FAIL, AlertStore

            AlertStore(self.root, self.bot_id).raise_alert(
                TYPE_PLAN_FAIL,
                detail or f"plan 连续失败 {self._error_streak} 次",
                streak=self._error_streak,
            )
        except Exception:  # noqa: BLE001
            pass

    def check(self) -> list[str]:
        """返回告警消息列表（空 = 健康）。"""
        if not self.path.exists():
            return ["health: no heartbeat yet"]
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return ["health: corrupt health.json"]
        alerts = []
        now = int(time.time())
        last = rec.get("last_heartbeat", 0)
        if now - last > self.heartbeat_crit:
            alerts.append(f"health: heartbeat {now - last}s ago (crit={self.heartbeat_crit})")
        if rec.get("llm_latency", 0) > self.llm_latency_warn:
            alerts.append(f"health: llm_latency {rec['llm_latency']}s > {self.llm_latency_warn}s")
        if rec.get("exec_latency", 0) > self.exec_latency_warn:
            alerts.append(f"health: exec_latency {rec['exec_latency']}s > {self.exec_latency_warn}s")
        if rec.get("error_streak", 0) >= self.error_warn:
            alerts.append(f"health: error_streak {rec['error_streak']} >= {self.error_warn}")
        return alerts
