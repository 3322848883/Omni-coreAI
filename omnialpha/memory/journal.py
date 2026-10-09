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


def _fld(obj: Any, key: str, default: Any = None) -> Any:
    """chip 在两条策略路径上形态不同（strategist 是 dataclass，persona 是 dict）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def tier1_journal_fields(chips: Any) -> dict:
    """契约 Tier 1 字段 → journal 的**按币**形态（两条策略路径共用，一处定义）。

    多币宇宙下每枚币有自己的 `region`/`invalidation`/`risk_pct`，只取 `chips[0]` 会让
    其余币的这些判定凭空消失 —— 而它们正是下一轮回放「当时为什么这么判」要用的（D-21）。
    原先 `loop.py` 与 `persona/runner.py` 各有一份近乎重复的 helper，两边已经漂移过一次，
    所以这里只留一份。

    返回：

    - `symbols`    本轮涉及哪些币（与 chips 同序、**不去重**，保证与 decisions 对齐）
    - `decisions`  与 symbols 同序的动作名
    - `tier1`      `{symbol: {region / rule_ids / risk_pct / invalidation_price / …}}`
                   （每币只收**非空**项；同币出现多次取第一条）
    - **单 chip 时把该币的 Tier-1 平铺到顶层** —— 单币 journal 的形态与改动前逐字一致
      （I11），`tier1` 只是增量。
    """
    symbols: list[str] = []
    decisions: list[str] = []
    tier1: dict[str, dict] = {}
    for c in (chips or []):
        sym = str(_fld(c, "symbol", "") or "").strip().upper()
        symbols.append(sym)
        decisions.append(str(_fld(c, "action", "") or ""))
        if not sym or sym in tier1:
            continue
        one: dict[str, Any] = {}
        if _fld(c, "region", ""):
            one["region"] = _fld(c, "region")
        if _fld(c, "rule_ids", None):
            one["rule_ids"] = list(_fld(c, "rule_ids"))
        for src, dst in (("risk_pct", "risk_pct"),
                         ("invalidation", "invalidation_price"),
                         ("time_stop_bars", "time_stop_bars"),
                         ("give_back_pct", "give_back_pct")):
            v = _fld(c, src, None)
            if v is not None:
                one[dst] = v
        if one:
            tier1[sym] = one
    if not symbols:
        return {}
    out: dict[str, Any] = {"symbols": symbols, "decisions": decisions}
    if len(symbols) == 1:
        out.update(tier1.get(symbols[0]) or {})
    if tier1:
        out["tier1"] = tier1
    return out


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

    def find(self, cycle_id: str) -> Optional[dict]:
        """按 `cycle_id` 取回一条决策记录（找不到返回 None）。

        `journal_lookup` 工具靠它：`[近期决策索引]` 只给 `cycle_id + decision` 一行，
        模型要某轮细节时按需取回 —— 这样「记忆」是**可检索**的，而不是把 N 轮正文
        都预载进 prompt（那是线性成本）。

        同一 `cycle_id` 出现多次时取**最新**一条：journal 是 append-only，
        同一轮重跑会再追加一行。
        """
        cid = str(cycle_id or "").strip()
        if not cid or not self.path.exists():
            return None
        try:
            lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        except Exception:  # noqa: BLE001
            return None
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if str(rec.get("cycle_id") or "") == cid:
                return rec
        return None

    def read_recent_summaries(self, n: int = 3) -> list[dict]:
        """读最近 n 条的摘要（进 prompt 的紧凑格式）。

        **不再 `[:30]` 截断** —— 那正是 `[近况]` 段只有 171 字符、而模型每轮写
        6K–76K 字符推理的原因。`build_context` 现在直接用 `read_recent` 自己组装，
        这个方法留在公开面上；再截断会把同一个 bug 从别的调用点引回来。
        """
        return [
            {
                "cycle_id": r.get("cycle_id", ""),
                "decision": r.get("decision", ""),
                "reasoning": r.get("reasoning", ""),
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
