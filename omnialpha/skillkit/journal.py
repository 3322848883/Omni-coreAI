"""skill 激活审计 journal（logs/skill_journal.jsonl）。

并发安全：进程内用 threading.Lock 串行化；跨进程用 OS 文件锁
（msvcrt.locking / fcntl.flock）——两个 bot 进程共写同一 journal 时不丢写。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

_WRITE_LOCK = threading.Lock()


def journal_path(root: Path, override: Optional[str] = None) -> Path:
    return Path(root) / (override or "logs/skill_journal.jsonl")


def _lock_exclusive(fh) -> None:
    """阻塞式获取 OS 排他锁（跨进程）。"""
    try:
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    except ImportError:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        pass  # 锁不可用时退化为仅进程内串行


def _unlock(fh) -> None:
    try:
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    except ImportError:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def append_journal(root: Path, record: dict[str, Any], override: Optional[str] = None) -> None:
    p = journal_path(root, override)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = dict(record)
        rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        lock_path = p.with_suffix(p.suffix + ".lock")
        with _WRITE_LOCK:  # 进程内串行
            lf = open(lock_path, "a+b")
            try:
                _lock_exclusive(lf)  # 跨进程串行
                with p.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                _unlock(lf)
                lf.close()
    except OSError:
        pass  # 审计失败不阻断主流程


def read_activation_freq(root: Path, override: Optional[str] = None) -> dict[str, int]:
    """激活次数（用于 catalog 降级排序）。"""
    p = journal_path(root, override)
    freq: dict[str, int] = {}
    if not p.is_file():
        return freq
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                sid = rec.get("skill_id")
                if sid:
                    freq[sid] = freq.get(sid, 0) + 1
    except OSError:
        pass
    return freq
