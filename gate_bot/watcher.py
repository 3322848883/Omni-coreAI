"""Inbox folder watcher: multi-bot JSON signal consumption."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import BotConfig
from .executor import ExecReport, Executor
from .gate_client import GateApiError
from .schema import SchemaError, parse_signal

log = logging.getLogger("gate_bot.watcher")


@dataclass
class ProjectPaths:
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config" / "bots"

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def done_dir(self) -> Path:
        return self.root / "archive" / "done"

    @property
    def failed_dir(self) -> Path:
        return self.root / "archive" / "failed"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> None:
        for p in (self.config_dir, self.inbox, self.done_dir, self.failed_dir, self.logs):
            p.mkdir(parents=True, exist_ok=True)

    def bot_inbox(self, bot_id: str) -> Path:
        path = self.inbox / bot_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def bot_done(self, bot_id: str) -> Path:
        path = self.done_dir / bot_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def bot_failed(self, bot_id: str) -> Path:
        path = self.failed_dir / bot_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def _pick_inbox_files(inbox: Path, limit: int) -> list[Path]:
    files = [
        p
        for p in inbox.iterdir()
        if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
    ]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[:limit]


def _take_file(path: Path) -> Optional[Path]:
    """Rename to a processing temp name to avoid double-take."""
    tmp = path.with_name(f".{path.name}.taking")
    try:
        os.replace(path, tmp)
        return tmp
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("take file failed %s: %s", path, e)
        return None


def process_file(path: Path, bot: BotConfig, paths: ProjectPaths, executor: Optional[Executor] = None) -> bool:
    """Process one JSON signal file. Returns True on success archive."""
    tmp = path
    original_name = path.name
    while original_name.startswith("."):
        original_name = original_name[1:]
    if original_name.endswith(".taking"):
        original_name = original_name[: -len(".taking")]
    if original_name.endswith(".staged.json"):
        original_name = original_name[: -len(".staged.json")]
        if not original_name.endswith(".json"):
            original_name += ".json"
    try:
        raw = tmp.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        _archive_failed(paths, bot.bot_id, tmp, original_name, f"invalid JSON: {e}", None)
        return False

    try:
        signal = parse_signal(data, default_label=bot.label_prefix or bot.bot_id)
        # expand check for max_orders
        from .schema import expand_signal

        intents = expand_signal(signal)
        if len(intents) > bot.max_orders_per_file:
            raise SchemaError(
                f"intents {len(intents)} exceed max_orders_per_file={bot.max_orders_per_file}"
            )
    except SchemaError as e:
        _archive_failed(paths, bot.bot_id, tmp, original_name, f"schema: {e}", data)
        return False

    if executor is None:
        try:
            client = bot.create_client()
        except GateApiError as e:
            _archive_failed(paths, bot.bot_id, tmp, original_name, f"credentials: {e}", data)
            return False
        executor = Executor(
            client,
            symbols_whitelist=bot.symbols or None,
            max_notional_usd=bot.max_notional_usd,
        )

    report: ExecReport = executor.execute_signal(signal)
    result = report.to_dict()
    if report.ok:
        dest = paths.bot_done(bot.bot_id) / original_name
        os.replace(tmp, dest)
        (dest.parent / f"{dest.name}.result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("done %s/%s", bot.bot_id, original_name)
        return True

    err = next((r.error for r in report.results if not r.ok), "unknown error")
    _archive_failed(paths, bot.bot_id, tmp, original_name, err, data, extra=result)
    return False


def _archive_failed(
    paths: ProjectPaths,
    bot_id: str,
    tmp: Path,
    original_name: str,
    error: str,
    data: object = None,
    extra: dict = None,
) -> None:
    dest = paths.bot_failed(bot_id) / original_name
    try:
        if tmp.exists():
            os.replace(tmp, dest)
        elif not dest.exists():
            # write empty placeholder if source vanished
            dest.write_text(tmp.read_text(encoding="utf-8") if tmp.exists() else "", encoding="utf-8")
    except OSError as e:
        log.error("archive failed for %s: %s", original_name, e)
    payload = {
        "error": error,
        "code": "EXEC_OR_SCHEMA",
        "message": error,
        "file": original_name,
        "bot_id": bot_id,
    }
    if extra:
        steps = extra.get("steps") or []
        payload["report"] = extra
        payload["partials"] = [s for s in steps if s.get("ok")]
    (dest.parent / f"{original_name}.error.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.error("failed %s/%s: %s", bot_id, original_name, error)


def run_bot_once(bot: BotConfig, paths: ProjectPaths) -> dict:
    inbox = paths.bot_inbox(bot.bot_id)
    files = _pick_inbox_files(inbox, bot.max_files_per_run)
    processed = {"picked": len(files), "ok": 0, "failed": 0}
    executor = None
    for path in files:
        taken = _take_file(path)
        if taken is None:
            continue
        if executor is None:
            try:
                executor = Executor(
                    bot.create_client(),
                    symbols_whitelist=bot.symbols or None,
                    max_notional_usd=bot.max_notional_usd,
                )
            except GateApiError as e:
                _archive_failed(paths, bot.bot_id, taken, path.name, f"credentials: {e}")
                processed["failed"] += 1
                continue
        if process_file(taken, bot, paths, executor=executor):
            processed["ok"] += 1
        else:
            processed["failed"] += 1
    return processed


def run_forever(bots: dict[str, BotConfig], paths: ProjectPaths, only: Optional[str] = None) -> None:
    paths.ensure()
    selected = {k: v for k, v in bots.items() if v.enabled and (only is None or k == only)}
    if not selected:
        raise SystemExit("no enabled bots to run")
    # use max poll among bots as sleep base
    interval = min((b.poll_interval_sec for b in selected.values()), default=2.0)
    log.info("watching bots: %s", sorted(selected))
    while True:
        for bot in selected.values():
            try:
                stats = run_bot_once(bot, paths)
                if stats["picked"]:
                    log.info("bot %s: %s", bot.bot_id, stats)
            except Exception as e:  # noqa: BLE001 — keep loop alive
                log.exception("bot %s crashed: %s", bot.bot_id, e)
        time.sleep(max(0.2, interval))
