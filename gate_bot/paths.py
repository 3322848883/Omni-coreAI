"""Per-bot filesystem layout (layout v2).

data/bots/<bot_id>/
  inbox/  archive/done/  archive/failed/  logs/  state/
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

LAYOUT_VERSION = 2
LAYOUT_MARK = ".layout_version"

SUBDIRS = ("inbox", "archive/done", "archive/failed", "logs", "state")


class BotPaths:
    def __init__(self, root: Path, bot_id: str):
        self.root = Path(root).resolve()
        self.bot_id = str(bot_id or "").strip()
        if not self.bot_id:
            raise ValueError("bot_id required")
        if "/" in self.bot_id or "\\" in self.bot_id or self.bot_id in (".", ".."):
            raise ValueError(f"invalid bot_id {bot_id!r}")
        self.base = self.root / "data" / "bots" / self.bot_id

    @property
    def inbox(self) -> Path:
        return self.base / "inbox"

    @property
    def archive_done(self) -> Path:
        return self.base / "archive" / "done"

    @property
    def archive_failed(self) -> Path:
        return self.base / "archive" / "failed"

    @property
    def logs(self) -> Path:
        return self.base / "logs"

    @property
    def state(self) -> Path:
        return self.base / "state"

    @property
    def lock_plan(self) -> Path:
        return self.state / "plan.lock"

    @property
    def lock_run(self) -> Path:
        return self.state / "run.lock"

    def ensure(self) -> "BotPaths":
        for rel in SUBDIRS:
            (self.base / rel).mkdir(parents=True, exist_ok=True)
        mark = self.state / LAYOUT_MARK
        if not mark.exists():
            mark.write_text(str(LAYOUT_VERSION), encoding="utf-8")
        return self

    def layout_version(self) -> int:
        try:
            return int((self.state / LAYOUT_MARK).read_text(encoding="utf-8").strip())
        except Exception:  # noqa: BLE001
            return 0


def bots_root(root: Path) -> Path:
    return Path(root).resolve() / "data" / "bots"


def list_bot_ids(root: Path) -> list[str]:
    base = bots_root(root)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def detect_legacy_layout(root: Path, bot_id: str) -> bool:
    """True when old-style paths still hold content for this bot."""
    root = Path(root).resolve()

    def _has(p: Path) -> bool:
        if not p.exists():
            return False
        if p.is_file():
            return True
        return any(p.iterdir())

    legacy = [
        root / "inbox" / bot_id,
        root / "history" / bot_id,
        root / "logs" / "trades" / f"{bot_id}.jsonl",
        root / "archive" / "done" / bot_id,
        root / "archive" / "failed" / bot_id,
    ]
    return any(_has(p) for p in legacy)


def bot_paths(root: Path, bot_id: str, create: bool = False) -> BotPaths:
    bp = BotPaths(root, bot_id)
    return bp.ensure() if create else bp
