"""CLI: python -m gate_bot run|once|process|status"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import load_all_bots, load_bot_config
from .gate_client import GateApiError
from .watcher import ProjectPaths, process_file, run_bot_once, run_forever


def _root_from_args(args) -> Path:
    return Path(args.root).resolve() if getattr(args, "root", None) else Path.cwd().resolve()


def cmd_status(args) -> int:
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    bots = load_all_bots(paths.config_dir)
    out = {"root": str(paths.root), "bots": []}
    for bot_id, bot in sorted(bots.items()):
        inbox_files = list(paths.bot_inbox(bot_id).glob("*.json"))
        done_files = list(paths.bot_done(bot_id).glob("*.json"))
        # exclude result.json
        done_files = [p for p in done_files if not p.name.endswith(".result.json")]
        failed_files = [
            p for p in paths.bot_failed(bot_id).glob("*.json") if not p.name.endswith(".error.json")
        ]
        out["bots"].append(
            {
                "bot_id": bot_id,
                "enabled": bot.enabled,
                "env": bot.env,
                "symbols": bot.symbols,
                "inbox": len(inbox_files),
                "done": len(done_files),
                "failed": len(failed_files),
            }
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_once(args) -> int:
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    bots = load_all_bots(paths.config_dir)
    selected = {k: v for k, v in bots.items() if v.enabled and (args.bot is None or k == args.bot)}
    if not selected:
        print("no enabled bots", file=sys.stderr)
        return 1
    summary = {}
    for bot_id, bot in selected.items():
        summary[bot_id] = run_bot_once(bot, paths)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(s.get("failed", 0) == 0 for s in summary.values()) else 2


def cmd_process(args) -> int:
    root = _root_from_args(args)
    paths = ProjectPaths(root)
    paths.ensure()
    bot_id = args.bot
    cfg_path = paths.config_dir / f"{bot_id}.yaml"
    if not cfg_path.exists():
        print(f"missing bot config: {cfg_path}", file=sys.stderr)
        return 1
    bot = load_bot_config(cfg_path)
    src = Path(args.file).resolve()
    if not src.exists():
        print(f"missing file: {src}", file=sys.stderr)
        return 1
    # stage a copy so the caller's source file is not moved away
    inbox = paths.bot_inbox(bot_id)
    staged = inbox / f".{src.name}.staged.json"
    staged.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    ok = process_file(staged, bot, paths, executor=None)
    # process_file renames using original_name derived from staged name; map result names
    return 0 if ok else 2


def cmd_run(args) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    bots = load_all_bots(paths.config_dir)
    try:
        run_forever(bots, paths, only=args.bot)
    except KeyboardInterrupt:
        print("bye")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gate_bot", description="Gate strategy signal bot")
    parser.add_argument("--root", default=None, help="project root (default cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="watch inbox forever")
    p_run.add_argument("--bot", default=None)
    p_run.set_defaults(func=cmd_run)

    p_once = sub.add_parser("once", help="one scan pass then exit")
    p_once.add_argument("--bot", default=None)
    p_once.set_defaults(func=cmd_once)

    p_proc = sub.add_parser("process", help="process a single JSON file")
    p_proc.add_argument("file")
    p_proc.add_argument("--bot", required=True)
    p_proc.set_defaults(func=cmd_process)

    p_st = sub.add_parser("status", help="show bots and backlog")
    p_st.set_defaults(func=cmd_status)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GateApiError as e:
        print(f"gate error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
