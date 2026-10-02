"""CLI: python -m omnialpha.metrics [score|scan]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="omnialpha.metrics")
    p.add_argument("cmd", nargs="?", default="score", choices=["score", "scan", "json"])
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    import os
    root = Path(
        args.root or os.environ.get("OMNIALPHA_ROOT") or Path.cwd()
    ).expanduser().resolve()
    from .scoreboard import build_scoreboard, format_table, write_scoreboard
    from .trials import scan_trials

    if args.cmd == "scan":
        recs = scan_trials(root)
        print(json.dumps({"new_trials": recs}, ensure_ascii=False, indent=2))
        return 0
    board = build_scoreboard(root)
    path = write_scoreboard(root, board)
    if args.cmd == "json":
        print(json.dumps(board, ensure_ascii=False, indent=2))
    else:
        print(format_table(board))
        print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
