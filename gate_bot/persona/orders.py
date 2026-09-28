"""共享订单库：data/shared/orders/<order_id>.json（共同记忆，跨 bot 读写）。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


def new_order_id() -> str:
    return "o-" + uuid.uuid4().hex[:12]


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
