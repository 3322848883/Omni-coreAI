# -*- coding: utf-8 -*-
"""Watchdog: keep `python -m gate_bot run` alive (restart on crash)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)


def main() -> int:
    ap = argparse.ArgumentParser(description="gate-signal-bot watchdog")
    ap.add_argument("--bot", default=None, help="only this bot id")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--restart-delay", type=float, default=3.0)
    ap.add_argument("--max-restarts-per-hour", type=int, default=20)
    args = ap.parse_args()

    cmd = [str(PY), "-m", "gate_bot", "run", "--root", args.root]
    if args.bot:
        cmd += ["--bot", args.bot]
    env = os.environ.copy()
    env["PYTHONPATH"] = args.root

    restarts: list[float] = []
    print(f"[watchdog] start: {' '.join(cmd)}", flush=True)
    while True:
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, cwd=args.root, env=env)
            code = proc.returncode
        except KeyboardInterrupt:
            print("[watchdog] stop", flush=True)
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[watchdog] spawn error: {e}", flush=True)
            code = -1
        print(f"[watchdog] gate_bot exited code={code} after {time.time()-t0:.1f}s", flush=True)
        now = time.time()
        restarts = [t for t in restarts if now - t < 3600]
        restarts.append(now)
        if len(restarts) > args.max_restarts_per_hour:
            print("[watchdog] too many restarts/hour — exit", flush=True)
            return 1
        time.sleep(args.restart_delay)


if __name__ == "__main__":
    sys.exit(main())
