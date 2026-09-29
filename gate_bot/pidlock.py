"""Single-instance file lock (orphan-safe).

Mutual exclusion comes from an OS file lock (``msvcrt.locking`` on Windows,
``fcntl.flock`` elsewhere). The OS releases it when the holding process dies,
so a crash/kill never leaves a stale lock behind.

The lock file's contents (owner PID) are diagnostics only — they are never
used to decide whether the lock is held. That avoids the PID-reuse trap:
Windows recycles PIDs quickly, and a dead holder's PID can belong to an
unrelated live process.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class PidLock:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def acquire(self) -> Optional["PidLock"]:
        if self._fh is not None:
            return self  # same instance already holds it
        try:
            fh = open(self.path, "a+", encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None
        try:
            self._lock_exclusive_nonblock(fh)
        except Exception:  # noqa: BLE001
            # held by another live process (or lock unavailable)
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
            return None
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except Exception:  # noqa: BLE001
            pass
        self._fh = fh
        return self

    @staticmethod
    def _lock_exclusive_nonblock(fh) -> None:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fh) -> None:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            self._unlock(fh)
        except Exception:  # noqa: BLE001
            pass
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
        # Do not unlink: Windows cannot delete a file another process may have
        # open, and the content is diagnostic-only (overwritten on next acquire).

    def __enter__(self) -> "PidLock":
        if self.acquire() is None:
            raise RuntimeError(f"lock held: {self.path}")
        return self

    def __exit__(self, *args) -> None:
        self.release()
