"""共享订单库：data/shared/orders/<order_id>.json（共同记忆，跨 bot 读写）。

扩展记忆字段（agent-memory）：reason/memory_refs/lifecycle/recent_events/invalidation。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# lifecycle 管理动作权重（recent_events 选取用，FinMem top-K 模式）
ACT_WEIGHTS = {
    "open": 10,
    "modify_sl": 8,
    "reduce_major": 7,
    "modify_tp": 5,
    "reduce": 4,
    "hold": 1,
}
RECENT_EVENTS_MAX = 5
EVENT_DECAY_HALF_LIFE = 10  # 轮


def new_order_id() -> str:
    return "o-" + uuid.uuid4().hex[:12]


def _event_weight(act: str, t_now: int, t_event: int) -> float:
    """weight × 时间衰减（半衰期 EVENT_DECAY_HALF_LIFE）。"""
    base = ACT_WEIGHTS.get(act, 2)
    age = max(0, t_now - t_event) / 3600  # 小时
    rounds = age / 0.25  # 15min = 0.25h
    decay = 0.5 ** (rounds / EVENT_DECAY_HALF_LIFE)
    return base * decay


class SharedOrderStore:
    """按 order_id 索引的共享订单库；原子写；保留 votes/reason/log。"""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / "data" / "shared" / "orders"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _lock_for(self, order_id: str) -> threading.Lock:
        key = str(order_id)
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _path(self, order_id: str) -> Path:
        oid = str(order_id or "").strip()
        if not oid or oid in (".", "..") or "/" in oid or "\\" in oid or ".." in oid:
            raise ValueError(f"invalid order_id {order_id!r}")
        if "\x00" in oid or not oid.isascii():
            raise ValueError(f"invalid order_id {order_id!r}")
        return self.dir / f"{oid}.json"

    def create(self, order: dict) -> dict:
        oid = str(order.get("order_id") or new_order_id())
        rec = dict(order)
        rec["order_id"] = oid
        rec.setdefault("votes", {})
        rec.setdefault("reason", {})
        rec.setdefault("log", [])
        # agent-memory 字段
        rec.setdefault("reason_text", "")
        rec.setdefault("memory_refs", [])
        rec.setdefault("lifecycle", [])
        rec.setdefault("recent_events", [])
        rec.setdefault("invalidation", [])
        rec["status"] = rec.get("status") or "open"
        rec["created_at"] = int(time.time())
        rec["updated_at"] = rec["created_at"]
        with self._lock_for(oid):
            self._write(self._path(oid), rec)
        return rec

    def get(self, order_id: str) -> Optional[dict]:
        p = self._path(order_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def update(self, order_id: str, **fields: Any) -> dict:
        with self._lock_for(order_id):
            rec = self.get(order_id)
            if rec is None:
                raise KeyError(f"order not found: {order_id}")
            rec.update(fields)
            rec["updated_at"] = int(time.time())
            self._write(self._path(order_id), rec)
            return rec

    def append_log(self, order_id: str, act: str, detail: str = "") -> dict:
        with self._lock_for(order_id):
            rec = self.get(order_id)
            if rec is None:
                raise KeyError(f"order not found: {order_id}")
            rec.setdefault("log", []).append({
                "t": int(time.time()), "act": act, "detail": detail,
            })
            rec["updated_at"] = int(time.time())
            self._write(self._path(order_id), rec)
            return rec

    def record_vote(self, order_id: str, cycle_id: str, bot_id: str, decision: str,
                    reasoning: str = "", confidence: float = 0.0) -> dict:
        """记录各人格立场（共同记忆）。"""
        with self._lock_for(order_id):
            rec = self.get(order_id)
            if rec is None:
                raise KeyError(f"order not found: {order_id}")
            rec.setdefault("votes", {})
            rec["votes"].setdefault(cycle_id, {})
            rec["votes"][cycle_id][bot_id] = {
                "decision": decision, "reasoning": reasoning, "confidence": confidence,
            }
            rec.setdefault("reason", {})
            if reasoning:
                rec["reason"][bot_id] = reasoning
            rec["updated_at"] = int(time.time())
            self._write(self._path(order_id), rec)
            return rec

    def list_open(self, group: Optional[str] = None) -> list[dict]:
        """列出 open 订单；可选按 group 过滤（多组隔离）。"""
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if rec.get("status") != "open":
                continue
            if group is not None and rec.get("group") != group:
                continue
            out.append(rec)
        return out

    # ── agent-memory: 订单上下文扩展 ──────────────────────

    def set_reason(self, order_id: str, reason_text: str) -> dict:
        """设置开仓理由（prompt 常驻）。"""
        return self.update(order_id, reason_text=str(reason_text or "")[:500])

    def add_lifecycle(self, order_id: str, act: str, detail: str = "",
                      by: str = "") -> dict:
        """追加管理事件（不可变 append-only 语义，但文件内是列表）。"""
        with self._lock_for(order_id):
            rec = self.get(order_id)
            if rec is None:
                raise KeyError(f"order not found: {order_id}")
            rec.setdefault("lifecycle", []).append({
                "t": int(time.time()), "act": act,
                "detail": str(detail or "")[:100], "by": str(by or ""),
            })
            rec["updated_at"] = int(time.time())
            self._write(self._path(order_id), rec)
            return rec

    def add_memory_ref(self, order_id: str, ref: str) -> dict:
        """记录决策引用的 journal 索引（FinPos 模式）。"""
        with self._lock_for(order_id):
            rec = self.get(order_id)
            if rec is None:
                raise KeyError(f"order not found: {order_id}")
            rec.setdefault("memory_refs", [])
            if ref and ref not in rec["memory_refs"]:
                rec["memory_refs"].append(ref)
            rec["updated_at"] = int(time.time())
            self._write(self._path(order_id), rec)
            return rec

    def add_invalidation(self, order_id: str, field: str,
                         old: Any, new: Any) -> dict:
        """标记旧假设失效（Memora FAMA 模式）。"""
        with self._lock_for(order_id):
            rec = self.get(order_id)
            if rec is None:
                raise KeyError(f"order not found: {order_id}")
            rec.setdefault("invalidation", []).append({
                "t": int(time.time()), "field": str(field),
                "old": str(old), "new": str(new),
            })
            rec["updated_at"] = int(time.time())
            self._write(self._path(order_id), rec)
            return rec

    def refresh_recent_events(self, order_id: str) -> dict:
        """从 lifecycle 选 top-5 关键决策事件（FinMem top-K + 衰减）。"""
        with self._lock_for(order_id):
            rec = self.get(order_id)
            if rec is None:
                raise KeyError(f"order not found: {order_id}")
            t_now = int(time.time())
            events = rec.get("lifecycle") or []
            scored = []
            for e in events:
                act = str(e.get("act") or "")
                t = int(e.get("t") or 0)
                w = _event_weight(act, t_now, t)
                scored.append((w, e))
            scored.sort(key=lambda x: -x[0])
            rec["recent_events"] = [
                {**e, "weight": round(w, 2)}
                for w, e in scored[:RECENT_EVENTS_MAX]
            ]
            rec["updated_at"] = t_now
            self._write(self._path(order_id), rec)
            return rec

    def get_order_context(self, order_id: str) -> Optional[dict]:
        """返回 prompt 可用的订单上下文摘要。"""
        rec = self.get(order_id)
        if rec is None or rec.get("status") != "open":
            return None
        return {
            "order_id": rec["order_id"],
            "symbol": rec.get("symbol", ""),
            "side": rec.get("side", ""),
            "entry_price": rec.get("entry_price"),
            "tp": rec.get("tp"),
            "sl": rec.get("sl"),
            "reason": rec.get("reason_text", ""),
            "recent_events": rec.get("recent_events", []),
            "memory_refs": rec.get("memory_refs", []),
            "invalidation": rec.get("invalidation", []),
        }

    def _write(self, path: Path, rec: dict) -> None:
        data = json.dumps(rec, ensure_ascii=False, indent=2)
        for attempt in range(10):
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.writing")
            try:
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, path)
                return
            except (PermissionError, OSError):
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                if attempt == 9:
                    raise
                time.sleep(0.005 * (attempt + 1))
