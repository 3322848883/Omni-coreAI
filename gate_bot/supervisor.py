"""Multi-bot supervisor: plan-loop + run per bot, PID locks, rate-limited restarts.

Modeled on pa-data-source/watchdog.py:
  - one supervisor process
  - per bot per component child
  - orphan-safe single-instance locks
  - restart storm protection (max N per rolling hour)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import load_all_bots
from .ledger import Ledger, default_ledger_path
from .paths import BotPaths, bot_paths, detect_legacy_layout
from .pidlock import PidLock

log_prefix = "[supervisor]"


@dataclass
class Child:
    bot_id: str
    component: str  # plan | run
    args: list[str]
    popen: Optional[subprocess.Popen] = None
    lock: Optional[PidLock] = None
    restarts: list[float] = field(default_factory=list)
    stopped: bool = False


class Supervisor:
    def __init__(self, root: Path, bot_ids: Optional[list[str]] = None,
                 max_restarts_per_hour: int = 5, check_interval_sec: float = 2.0,
                 auto_migrate: bool = False):
        self.root = Path(root).resolve()
        self.bot_ids = bot_ids
        self.max_restarts = max_restarts_per_hour
        self.check_interval = check_interval_sec
        self.auto_migrate = auto_migrate
        self.children: list[Child] = []
        self._stop = False
        self.ledger = Ledger(default_ledger_path(self.root))
        self._py = self._windowless_python()

    def _log(self, msg: str) -> None:
        print(f"{log_prefix} {msg}", flush=True)

    def _windowless_python(self) -> str:
        """Prefer pythonw.exe on Windows so children do not open consoles."""
        exe = Path(sys.executable)
        if os.name == "nt":
            w = exe.parent / "pythonw.exe"
            if w.exists():
                return str(w)
        return str(exe)

    def prepare(self) -> None:
        bots = load_all_bots(self.root / "config" / "bots")
        selected = {b: cfg for b, cfg in bots.items() if self.bot_ids is None or b in self.bot_ids}
        for bid, cfg in selected.items():
            if not getattr(cfg, "enabled", True):
                continue
            if detect_legacy_layout(self.root, bid):
                if self.auto_migrate:
                    from .migrate import migrate_bot

                    self._log(f"migrate {bid}: {migrate_bot(self.root, bid)}")
                else:
                    self._log(f"WARNING {bid}: legacy layout detected — run: python -m gate_bot migrate")
            bp = bot_paths(self.root, bid, create=True)
            runtime = dict(getattr(cfg, "strategist", {}) or {}).get("runtime") or {}
            if runtime.get("plan_loop", True):
                self.children.append(Child(bid, "plan", [
                    self._py, "-m", "gate_bot", "--root", str(self.root),
                    "plan-loop", "--bot", bid,
                ]))
            if runtime.get("run", True):
                self.children.append(Child(bid, "run", [
                    self._py, "-m", "gate_bot", "--root", str(self.root),
                    "run", "--bot", bid,
                ]))
            self._log(f"armed {bid} → {bp.base}")

    def _start(self, ch: Child) -> bool:
        bp = bot_paths(self.root, ch.bot_id, create=True)
        lock_path = bp.lock_plan if ch.component == "plan" else bp.lock_run
        lock = PidLock(lock_path)
        if lock.acquire() is None:
            self._log(f"skip {ch.bot_id}/{ch.component}: lock held")
            return False
        ch.lock = lock
        log = bp.logs / f"{ch.component}.out"
        err = bp.logs / f"{ch.component}.err"
        log.parent.mkdir(parents=True, exist_ok=True)
        out_fh = open(log, "ab")
        err_fh = open(err, "ab")
        env = os.environ.copy()
        # child CLI skips PidLock — supervisor already holds lock_plan/lock_run
        env["GATE_LOCK_HELD"] = "1"
        flags = 0
        if os.name == "nt":
            # windowless: DETACHED | NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            flags = 0x00000008 | 0x00000200 | 0x08000000
        else:
            env.setdefault("PYTHONUNBUFFERED", "1")
        ch.popen = subprocess.Popen(
            ch.args, cwd=str(self.root), env=env,
            stdout=out_fh, stderr=err_fh, creationflags=flags,
        )
        self._log(f"started {ch.bot_id}/{ch.component} pid={ch.popen.pid}")
        self.ledger.heartbeat(ch.bot_id, ch.component, pid=ch.popen.pid, detail="started")
        return True

    def _allow_restart(self, ch: Child) -> bool:
        now = time.time()
        ch.restarts = [t for t in ch.restarts if now - t < 3600]
        if len(ch.restarts) >= self.max_restarts:
            ch.stopped = True
            self._log(f"HOLD {ch.bot_id}/{ch.component}: >={self.max_restarts} restarts/hour")
            self.ledger.heartbeat(ch.bot_id, ch.component, pid=0, detail="held_restart_storm")
            return False
        ch.restarts.append(now)
        return True

    def _reap(self) -> None:
        for ch in self.children:
            if ch.stopped:
                continue
            if ch.popen is None:
                # retry start (e.g. lock was briefly held); do not burn restart budget
                self._start(ch)
                continue
            rc = ch.popen.poll()
            if rc is None:
                self.ledger.heartbeat(ch.bot_id, ch.component, pid=ch.popen.pid, detail="alive")
                continue
            self._log(f"exit {ch.bot_id}/{ch.component} rc={rc}")
            if ch.lock:
                ch.lock.release()
                ch.lock = None
            ch.popen = None
            if not self._stop and self._allow_restart(ch):
                time.sleep(min(5.0, self.check_interval * 2))
                self._start(ch)

    def run_forever(self) -> None:
        self.prepare()
        if not self.children:
            self._log("no children armed; exiting")
            return

        def _sig(*_a):
            self._stop = True

        try:
            signal.signal(signal.SIGINT, _sig)
            signal.signal(signal.SIGTERM, _sig)
        except Exception:  # noqa: BLE001
            pass

        for ch in self.children:
            self._start(ch)
        self._log(f"supervising {len(self.children)} components")
        try:
            while not self._stop:
                self._reap()
                time.sleep(self.check_interval)
        finally:
            self._stop = True
            for ch in self.children:
                if ch.popen and ch.popen.poll() is None:
                    try:
                        ch.popen.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                if ch.lock:
                    ch.lock.release()
            self.ledger.close()
            self._log("stopped")

    def status(self) -> list[dict]:
        rows = []
        for ch in self.children:
            pid = ch.popen.pid if ch.popen and ch.popen.poll() is None else None
            rows.append({
                "bot_id": ch.bot_id,
                "component": ch.component,
                "pid": pid,
                "running": pid is not None,
                "stopped": ch.stopped,
                "restarts_1h": len([t for t in ch.restarts if time.time() - t < 3600]),
            })
        return rows
