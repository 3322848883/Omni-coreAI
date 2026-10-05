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
                 heartbeat_crit: int = 600,
                 role: str = "plan"):
        self.root = Path(root)
        self.bot_id = bot_id
        # plan-loop 与 run 是两个进程，而 heartbeat() 是**整体覆盖**语义
        # （未传的指标视为无）—— 共用同一个文件会互相抹掉字段。
        # 所以各写各的：plan 仍用 health.json（保持兼容），run 用 health.run.json；
        # check() 两边合并读。
        self.role = (role or "plan").strip().lower() or "plan"
        state = self.root / "data" / "bots" / bot_id / "state"
        self.path = state / ("health.json" if self.role == "plan" else f"health.{self.role}.json")
        self.run_path = state / "health.run.json"
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

    def _load_merged(self) -> dict:
        """plan 侧记录 + run 侧记录（exec_latency 等）合并。

        两个进程各写各的文件，这里合并成一份视图；同名字段以 plan 侧为准。
        """
        rec = dict(self._load())
        if self.role == "plan" and self.run_path.exists():
            try:
                run_rec = json.loads(self.run_path.read_text(encoding="utf-8"))
                if isinstance(run_rec, dict):
                    for k, v in run_rec.items():
                        rec.setdefault(k, v)
            except Exception:  # noqa: BLE001
                pass
        return rec

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

    def record_exec_result(self, failed: int) -> int:
        """run 侧：按批记录执行结果，返回连续失败批次数。

        执行层的连续失败是 run 侧独有的信号（plan-loop 看不到执行结果）。
        与 `error_streak` 一样按 role 落盘，两个进程各写自己的文件、不互相抹掉。
        """
        rec = self._load()
        streak = int(rec.get("exec_fail_streak") or 0)
        streak = streak + 1 if int(failed or 0) > 0 else 0
        rec.update({
            "ts": int(time.time()),
            "last_heartbeat": int(time.time()),
            "exec_fail_streak": streak,
        })
        self.path.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return streak

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

    def current_fail_streak(self) -> int:
        """当前连续失败次数（plan 侧 error_streak 与 run 侧 exec_fail_streak 取大）。

        供**执行层**做安全模式判定用 —— 原先这两个计数只有 `check()` 读，
        而 `check()` 在生产里无人调用（见 `docs/compose/spec/agent-memory.md`
        对 HealthMonitor 装饰性的记录）。安全模式需要一个「随时可查」的入口。
        """
        rec = self._load_merged()
        try:
            return max(int(rec.get("error_streak") or 0),
                       int(rec.get("exec_fail_streak") or 0))
        except (TypeError, ValueError):
            return 0

    def check(self) -> list[str]:
        """返回告警消息列表（空 = 健康）。plan / run 两侧记录合并后判定。"""
        if not self.path.exists() and not self.run_path.exists():
            return ["health: no heartbeat yet"]
        rec = self._load_merged()
        if not rec:
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
        if rec.get("exec_fail_streak", 0) >= self.error_warn:
            alerts.append(
                f"health: exec_fail_streak {rec['exec_fail_streak']} >= {self.error_warn}"
            )
        return alerts
