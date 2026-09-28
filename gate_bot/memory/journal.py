"""事件溯源决策日志：data/bots/<bot>/state/memory_journal.jsonl（append-only 不可变）。

每轮追加一条记录，含决策/理由/记忆引用/缓存命中/执行结果。
合规级审计（MiFID II 时间序列 + 算法 ID），崩溃恢复，近况摘要来源。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional


class MemoryJournal:
    """append-only 决策日志。不提供 update/delete（不可变）。"""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root: Path, bot_id: str):
        self.root = Path(root)
        self.bot_id = bot_id
        self.path = self.root / "data" / "bots" / bot_id / "state" / "memory_journal.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path)
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            self._lock = self._locks[key]

    def append(self, *, cycle_id: str, decision: str, reasoning: str = "",
               memory_refs: Optional[list[str]] = None,
               snapshot_digest: str = "",
               llm_model: str = "",
               prompt_cache_hit_tokens: int = 0,
               executed: bool = False,
               exec_result: Optional[dict] = None,
               **extra: Any) -> dict:
        """追加一条决策日志（不可变）。返回写入的记录。"""
        rec = {
            "ts": int(time.time()),
            "cycle_id": str(cycle_id),
            "decision": str(decision),
            "reasoning": str(reasoning or "")[:200],
            "memory_refs": list(memory_refs or []),
            "snapshot_digest": str(snapshot_digest or ""),
            "llm_model": str(llm_model or ""),
            "prompt_cache_hit_tokens": int(prompt_cache_hit_tokens or 0),
            "executed": bool(executed),
            "exec_result": dict(exec_result or {}),
        }
        if extra:
            rec.update(extra)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def read_recent(self, n: int = 3) -> list[dict]:
        """读最近 n 条（近况窗口来源）。"""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        except Exception:  # noqa: BLE001
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        return out

    def read_recent_summaries(self, n: int = 3) -> list[dict]:
        """读最近 n 条的摘要（进 prompt 的紧凑格式）。"""
        return [
            {
                "cycle_id": r.get("cycle_id", ""),
                "decision": r.get("decision", ""),
                "reasoning": r.get("reasoning", "")[:30],
            }
            for r in self.read_recent(n)
        ]

    def count(self) -> int:
        if not self.path.exists():
            return 0
        try:
            return len(self.path.read_text(encoding="utf-8").strip().splitlines())
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def snapshot_digest(snapshot: Any) -> str:
        """快照摘要（sha256 前 12 位），用于审计不存全量。"""
        raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:12]
