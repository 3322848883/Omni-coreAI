"""Single-instance PID file lock (orphan-safe)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


class PidLock:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def acquire(self) -> Optional["PidLock"]:
        # atomic create; if exists, steal only when owner pid is dead
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            return self
        except FileExistsError:
            pass
        try:
            old = int((self.path.read_text(encoding="utf-8") or "0").strip() or "0")
        except Exception:  # noqa: BLE001
            old = 0
        if old and _pid_alive(old) and old != os.getpid():
            return None
        # dead/empty/stale → take over
        try:
            self.path.unlink(missing_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        except Exception:  # noqa: BLE001
            return None
        return self

    def release(self) -> None:
        try:
            if self.path.exists():
                txt = self.path.read_text(encoding="utf-8").strip()
                if txt == str(os.getpid()):
                    self.path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "PidLock":
        if self.acquire() is None:
            raise RuntimeError(f"lock held: {self.path}")
        return self

    def __exit__(self, *args) -> None:
        self.release()
