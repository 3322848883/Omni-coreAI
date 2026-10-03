"""缓存护栏：前缀稳定校验 + prompt_cache_hit_tokens 监控。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


class CacheGuard:
    """监控 prompt 缓存命中率（DeepSeek prompt_cache_hit_tokens）。"""

    def __init__(self, root: Path, bot_id: str):
        self.root = Path(root)
        self.bot_id = bot_id
        self.path = self.root / "data" / "bots" / bot_id / "state" / "cache_stats.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, hit_tokens: int, total_tokens: int, model: str = "") -> dict:
        """记录一轮缓存命中。"""
        rec = {
            "ts": int(time.time()),
            "hit": int(hit_tokens),
            "total": int(total_tokens),
            "hit_rate": round(hit_tokens / total_tokens, 3) if total_tokens else 0,
            "model": model,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def recent_hit_rate(self, n: int = 20) -> float:
        """最近 n 轮平均缓存命中率。"""
        if not self.path.exists():
            return 0.0
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        rates = []
        for line in lines[-n:]:
            try:
                rates.append(json.loads(line).get("hit_rate", 0))
            except Exception:  # noqa: BLE001
                continue
        return round(sum(rates) / len(rates), 3) if rates else 0.0

    @staticmethod
    def validate_prefix_stable(prev_system: str, curr_system: str) -> bool:
        """校验 system prompt 前缀未变（缓存命中的前提）。"""
        return prev_system == curr_system

    def check_prefix(self, system_prompt: str) -> bool:
        """与上一轮比对 system prompt 前缀，返回是否稳定。

        缓存命中的前提是前缀**逐字稳定**（设计 S2.6）。上一轮的前缀摘要存
        `state/cache_prefix.sha256`；只有变化时才重写（首次运行视为稳定）。
        """
        import hashlib
        cur = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        p = self.path.with_name("cache_prefix.sha256")
        prev = ""
        try:
            if p.exists():
                prev = p.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            prev = ""
        if prev != cur:
            try:
                p.write_text(cur, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        return not prev or prev == cur
