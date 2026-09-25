"""Migrate legacy per-type layout to per-bot layout v2 + import trades jsonl."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .ledger import Ledger, default_ledger_path
from .paths import BotPaths, LAYOUT_VERSION, detect_legacy_layout


def _move_tree(src: Path, dst: Path, copy: bool = False) -> int:
    if not src.exists():
        return 0
    n = 0
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if target.exists():
            continue
        if copy:
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        else:
            shutil.move(str(item), str(target))
        n += 1
    return n


def migrate_bot(root: Path, bot_id: str, dry_run: bool = False, copy: bool = False) -> dict[str, Any]:
    root = Path(root).resolve()
    bp = BotPaths(root, bot_id)
    report: dict[str, Any] = {"bot_id": bot_id, "dry_run": dry_run, "moved": {}, "imported_trades": 0}
    if not detect_legacy_layout(root, bot_id) and bp.layout_version() >= LAYOUT_VERSION:
        report["skipped"] = "already v2"
        return report

    steps = [
        ("inbox", root / "inbox" / bot_id, bp.inbox),
        ("archive_done", root / "archive" / "done" / bot_id, bp.archive_done),
        ("archive_failed", root / "archive" / "failed" / bot_id, bp.archive_failed),
        ("state", root / "history" / bot_id, bp.state),
        ("logs_root", root / "logs" / "trades" / f"{bot_id}.jsonl", bp.logs / "trades.jsonl"),
    ]
    if dry_run:
        for name, src, dst in steps:
            report["moved"][name] = {"src": str(src), "dst": str(dst), "exists": src.exists()}
        return report

    bp.ensure()
    for name, src, dst in steps:
        if name == "logs_root":
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst) if copy else shutil.move(str(src), str(dst))
                report["moved"][name] = 1
            continue
        report["moved"][name] = _move_tree(src, dst, copy=copy)

    # import trades jsonl → sqlite once
    mark = bp.state / ".jsonl_imported"
    jsonl = bp.logs / "trades.jsonl"
    if jsonl.exists() and not mark.exists():
        led = Ledger(default_ledger_path(root))
        try:
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if row.get("type") and row.get("type") != "execution":
                    led.insert_plan(
                        bot_id,
                        cycle_id=row.get("plan_cycle") or row.get("cycle_id"),
                        trigger=row.get("source") or row.get("type"),
                        orders=len(row.get("chips") or row.get("orders") or []),
                        reasoning=(row.get("reasoning") or "")[:200],
                        raw=row,
                    )
                else:
                    led.insert_trade(
                        bot_id,
                        plan_cycle=row.get("plan_cycle"),
                        action="execution",
                        ok=bool(row.get("ok")),
                        steps=row.get("steps"),
                        source=row.get("source") or "import",
                        ts=_ts(row.get("ts")),
                    )
                    report["imported_trades"] += 1
        finally:
            led.close()
        mark.write_text(str(int(time.time())), encoding="utf-8")
    return report


def _ts(v: Any) -> float:
    try:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            from datetime import datetime

            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        pass
    import time

    return time.time()


def migrate_all(root: Path, dry_run: bool = False, bot_ids: list[str] | None = None) -> list[dict]:
    root = Path(root).resolve()
    ids = set(bot_ids or [])
    # discover legacy ids
    for base in (root / "inbox", root / "history", root / "logs" / "trades"):
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                ids.add(p.name)
            elif p.is_file() and p.suffix == ".jsonl":
                ids.add(p.stem)
    return [migrate_bot(root, bid, dry_run=dry_run) for bid in sorted(ids)]
