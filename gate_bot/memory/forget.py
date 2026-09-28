"""遗忘机制：TTL 归档 + journal 清理。"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path


def archive_journal(root: Path, bot_id: str, max_age_days: int = 90) -> int:
    """归档超龄 journal 条目到 .gz。返回归档条数。

    90 天内的保留，更早的压缩归档（保留可审计，不进 prompt）。
    """
    journal_path = root / "data" / "bots" / bot_id / "state" / "memory_journal.jsonl"
    if not journal_path.exists():
        return 0
    cutoff = int(time.time()) - max_age_days * 86400
    lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    keep, archive = [], []
    for line in lines:
        try:
            rec = json.loads(line)
            if rec.get("ts", 0) < cutoff:
                archive.append(line)
            else:
                keep.append(line)
        except Exception:  # noqa: BLE001
            keep.append(line)
    if not archive:
        return 0
    # 归档写 .gz
    archive_path = journal_path.with_suffix(f".{int(time.time())}.jsonl.gz")
    with gzip.open(archive_path, "wt", encoding="utf-8") as f:
        f.write("\n".join(archive) + "\n")
    # 保留写回
    journal_path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return len(archive)


def cleanup_closed_orders(root: Path, max_age_days: int = 365) -> int:
    """删除超龄已平仓订单文件（默认 365 天）。返回删除数。"""
    orders_dir = root / "data" / "shared" / "orders"
    if not orders_dir.exists():
        return 0
    cutoff = int(time.time()) - max_age_days * 86400
    removed = 0
    for p in orders_dir.glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if rec.get("status") != "closed":
            continue
        updated = rec.get("updated_at", 0)
        if updated < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed
