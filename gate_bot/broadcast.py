"""信号广播：一信号 → 多 bot inbox（配置驱动，AI 碰不到目标列表）。

config/broadcast.yaml:
    routes:
      - name: multi-scalp
        from: signal-feed          # 源 bot_id
        to: [gate-btc, okx-btc]    # 目标 bot_id 列表（1/2/N 个）
        enabled: true

信号 JSON 里的 targets/exchanges 字段一律忽略（不可注入目标）。
分发逐个校验写入，任一失败报错；成功归档 broadcast-done，失败归档 broadcast-failed。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .paths import BotPaths


class BroadcastError(Exception):
    pass


@dataclass
class Route:
    name: str
    src: str
    targets: list[str]
    enabled: bool = True


@dataclass
class BroadcastConfig:
    routes: list[Route] = field(default_factory=list)
    poll_interval_sec: float = 2.0
    max_files_per_run: int = 50


def load_broadcast_config(path: Path) -> BroadcastConfig:
    """解析 broadcast.yaml；目标 bot 名可为任意子集（1/2/N）。"""
    p = Path(path)
    if not p.exists():
        raise BroadcastError(f"broadcast config not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BroadcastError(f"broadcast config must be a mapping: {p}")
    routes = []
    for i, r in enumerate(data.get("routes") or []):
        if not isinstance(r, dict):
            raise BroadcastError(f"routes[{i}] must be a mapping")
        src = str(r.get("from") or "").strip()
        to = [str(x).strip() for x in (r.get("to") or []) if str(x).strip()]
        if not src:
            raise BroadcastError(f"routes[{i}].from required")
        if not to:
            raise BroadcastError(f"routes[{i}].to must list >=1 target bot")
        name = str(r.get("name") or f"{src}->{','.join(to)}")
        routes.append(Route(name=name, src=src, targets=to, enabled=bool(r.get("enabled", True))))
    return BroadcastConfig(
        routes=routes,
        poll_interval_sec=float(data.get("poll_interval_sec") or 2.0),
        max_files_per_run=int(data.get("max_files_per_run") or 50),
    )


def _check_bot_id(bot_id: str, role: str) -> str:
    """bot_id 白名单校验（防路径穿越）；复用 BotPaths 的规则。"""
    bid = str(bot_id or "").strip()
    if not bid:
        raise BroadcastError(f"{role} bot_id required")
    if "/" in bid or "\\" in bid or bid in (".", "..") or ".." in bid:
        raise BroadcastError(f"{role} invalid bot_id {bid!r}")
    return bid


def validate_routes(cfg: BroadcastConfig, known_bots: set[str]) -> None:
    """源与目标 bot 都必须存在；目标不可等于源；bot_id 防穿越。"""
    for r in cfg.routes:
        if not r.enabled:
            continue
        _check_bot_id(r.src, f"route {r.name!r} from")
        for b in r.targets:
            _check_bot_id(b, f"route {r.name!r} to")
        missing_from = r.src not in known_bots
        if missing_from:
            raise BroadcastError(
                f"route {r.name!r}: source bot not found: {r.src!r} (known: {sorted(known_bots)})"
            )
        missing = [b for b in r.targets if b not in known_bots]
        if missing:
            raise BroadcastError(
                f"route {r.name!r}: target bot not found: {missing} (known: {sorted(known_bots)})"
            )
        if r.src in r.targets:
            raise BroadcastError(f"route {r.name!r}: source cannot be a target of itself")


def _pick_files(inbox: Path, limit: int) -> list[Path]:
    if not inbox.exists():
        return []
    files = [
        p for p in inbox.iterdir()
        if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
    ]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[:limit]


def _atomic_write(dst: Path, text: str) -> None:
    """原子落盘：唯一临时名 + rename，防目标半读与并发碰撞。"""
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{time.time_ns()}.writing")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dst)


def _clean_name(name: str) -> str:
    """去掉 .taking / .writing 临时后缀，还原原始信号名。"""
    n = name
    if n.startswith("."):
        n = n[1:]
    for suf in (".taking", ".writing"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n


def _archive(src_file: Path, archive_dir: Path, out_name: str) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / out_name
    if dst.exists():
        dst = archive_dir / f"{int(time.time())}-{out_name}"
    os.replace(src_file, dst)


class Broadcaster:
    def __init__(self, root: Path, cfg: BroadcastConfig, known_bots: Optional[set[str]] = None):
        self.root = Path(root)
        self.cfg = cfg
        self.known_bots = known_bots
        self.log_path = self.root / "data" / "broadcast" / "log.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, rec: dict) -> None:
        rec = dict(rec, ts=int(time.time()))
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _bot_inbox(self, bot_id: str) -> Path:
        # 走 BotPaths：唯一 bot_id 净化入口
        return BotPaths(self.root, bot_id).inbox

    def _archive_done(self, bot_id: str) -> Path:
        return BotPaths(self.root, bot_id).base / "archive" / "broadcast-done"

    def _archive_failed(self, bot_id: str) -> Path:
        return BotPaths(self.root, bot_id).base / "archive" / "broadcast-failed"

    def broadcast_one(self, route: Route, src_file: Path, out_name: str) -> dict:
        """把一条信号分发到 route 的所有目标；逐个校验，任一失败返回 failed。"""
        text = src_file.read_text(encoding="utf-8")
        # 安全：忽略信号里的 targets（AI 不可注入）
        injected = None
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                tg = obj.get("targets")
                ex = obj.get("exchanges")
                if tg or ex:
                    vals = []
                    if isinstance(tg, list):
                        vals += [str(x) for x in tg]
                    elif tg:
                        vals.append(str(tg))
                    if isinstance(ex, list):
                        vals += [str(x) for x in ex]
                    elif ex:
                        vals.append(str(ex))
                    injected = sorted(set(vals))
        except Exception:  # noqa: BLE001
            pass

        delivered, failed = [], []
        for bot in route.targets:
            try:
                dst_dir = self._bot_inbox(bot)
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = dst_dir / out_name
                if dst.exists():
                    delivered.append({"bot": bot, "status": "exists"})
                    continue
                _atomic_write(dst, text)
                if not dst.exists():
                    raise BroadcastError("write vanished")
                json.loads(dst.read_text(encoding="utf-8"))
                delivered.append({"bot": bot, "status": "ok"})
            except Exception as e:  # noqa: BLE001
                failed.append({"bot": bot, "error": f"{type(e).__name__}: {e}"})

        rec = {
            "route": route.name,
            "from": route.src,
            "file": out_name,
            "targets": route.targets,
            "delivered": delivered,
            "failed": failed,
            "ignored_targets_in_signal": injected,
        }
        self._log(rec)
        return rec

    def run_once(self) -> dict:
        """扫描源 inbox，同源多路由一次取件、整批分发，避免同源饥饿。"""
        summary = {"broadcast": 0, "ok": 0, "partial": 0, "failed": 0, "records": []}
        # 按源分组路由（同源多路由：取一次件，逐路由分发）
        by_src: dict[str, list[Route]] = {}
        for r in self.cfg.routes:
            if r.enabled:
                by_src.setdefault(r.src, []).append(r)

        for src, routes in by_src.items():
            inbox = self._bot_inbox(src)
            for f in _pick_files(inbox, self.cfg.max_files_per_run):
                # 原子取走，防双广播
                tmp = f.with_name(f".{f.name}.taking")
                try:
                    os.replace(f, tmp)
                except FileNotFoundError:
                    continue
                out_name = _clean_name(f.name)
                any_fail = False
                any_ok = False
                for route in routes:
                    try:
                        rec = self.broadcast_one(route, tmp, out_name=out_name)
                    except Exception as e:  # noqa: BLE001
                        rec = {"route": route.name, "from": src, "file": out_name,
                               "error": str(e), "delivered": [],
                               "failed": [{"bot": "*", "error": str(e)}]}
                        self._log(rec)
                    summary["broadcast"] += 1
                    summary["records"].append(rec)
                    if rec.get("failed"):
                        any_fail = True
                    if rec.get("delivered"):
                        any_ok = True
                # 源归档：任一失败 → broadcast-failed（可重投）；全成 → broadcast-done
                try:
                    if any_fail:
                        _archive(tmp, self._archive_failed(src), out_name)
                    else:
                        _archive(tmp, self._archive_done(src), out_name)
                except Exception:  # noqa: BLE001
                    _archive(tmp, self._archive_done(src), out_name)
                if any_fail:
                    summary["partial" if any_ok else "failed"] += 1
                else:
                    summary["ok"] += 1
        return summary

    def run_forever(self) -> None:
        print(f"broadcast start: {len([r for r in self.cfg.routes if r.enabled])} routes → {self.log_path}")
        while True:
            try:
                s = self.run_once()
                if s["broadcast"]:
                    print(f"{time.strftime('%H:%M:%S')} broadcast={s['broadcast']} ok={s['ok']} "
                          f"partial={s['partial']} failed={s['failed']}")
            except Exception as e:  # noqa: BLE001
                print(f"{time.strftime('%H:%M:%S')} broadcast error: {e}")
            time.sleep(self.cfg.poll_interval_sec)
