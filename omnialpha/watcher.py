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

log = logging.getLogger("omnialpha.watcher")


def _alert_store(paths: "ProjectPaths", bot_id: str):
    """P0.4 告警落盘（失败不阻塞执行）。"""
    try:
        from .monitoring import AlertStore

        return AlertStore(paths.root, bot_id)
    except Exception:  # noqa: BLE001
        return None


@dataclass
class ProjectPaths:
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config" / "bots"

    @property
    def inbox(self) -> Path:
        return self.root / "data" / "bots"

    @property
    def done_dir(self) -> Path:
        return self.root / "data" / "bots"

    @property
    def failed_dir(self) -> Path:
        return self.root / "data" / "bots"

    @property
    def logs(self) -> Path:
        return self.root / "data" / "bots"

    def ensure(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "bots").mkdir(parents=True, exist_ok=True)

    def bot_paths(self, bot_id: str, create: bool = True):
        from .paths import bot_paths as _bp

        return _bp(self.root, bot_id, create=create)

    def bot_inbox(self, bot_id: str) -> Path:
        return self.bot_paths(bot_id).inbox

    def bot_done(self, bot_id: str) -> Path:
        return self.bot_paths(bot_id).archive_done

    def bot_failed(self, bot_id: str) -> Path:
        return self.bot_paths(bot_id).archive_failed


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
            position_policy=bot.position_policy,
            default_replace=bot.default_replace,
            order_scope=getattr(bot, "order_scope", "own"),
            require_sl=getattr(bot, "require_sl", True),
            account_risk=getattr(bot, "account_risk", None) or {},
            label_prefix=getattr(bot, "label_prefix", "") or bot.bot_id,
            alert_store=_alert_store(paths, bot.bot_id),
            root=paths.root,
            bot_id=bot.bot_id,
        )

    report: ExecReport = executor.execute_signal(signal)
    result = report.to_dict()
    try:
        from .tradelog import TradeLogger, trade_log_path

        TradeLogger(trade_log_path(paths.root, bot.bot_id)).log_execution(
            bot.bot_id,
            (signal.meta or {}),
            result,
            source="watcher",
            env=getattr(bot, "env", "live") or "live",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("trade log write failed: %s", e)
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


def reconcile_protection(client, symbols: list[str], prefix: str = "") -> list[str]:
    """Startup check: flag positions that have no owned TP/SL (naked risk).

    Returns warning strings; does not place orders (caller decides).
    """
    warnings = []
    try:
        positions = client.get_positions() or []
    except Exception as e:  # noqa: BLE001
        return [f"reconcile: cannot read positions: {e}"]
    for p in positions:
        if int(p.get("size") or 0) == 0:
            continue
        sym = p.get("contract") or ""
        if symbols and sym not in symbols:
            continue
        has_protect = False
        try:
            for po in client.list_price_orders(sym) or []:
                text = str((po.get("initial") or {}).get("text") or po.get("text") or "")
                if prefix and not text.startswith(prefix):
                    continue
                if "-sl" in text or "-tp" in text:
                    has_protect = True
                    break
        except Exception:  # noqa: BLE001
            pass
        if not has_protect:
            warnings.append(
                f"reconcile: {sym} position size={p.get('size')} has no owned TP/SL (prefix={prefix or '*'})"
            )
    return warnings


def run_bot_once(bot: BotConfig, paths: ProjectPaths) -> dict:
    t0 = time.time()
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
                    position_policy=bot.position_policy,
                    default_replace=bot.default_replace,
                    order_scope=getattr(bot, "order_scope", "own"),
                    require_sl=getattr(bot, "require_sl", True),
                    account_risk=getattr(bot, "account_risk", None),
                    label_prefix=getattr(bot, "label_prefix", "") or bot.bot_id,
                    alert_store=_alert_store(paths, bot.bot_id),
                    root=paths.root,
                    bot_id=bot.bot_id,
                )
            except GateApiError as e:
                _archive_failed(paths, bot.bot_id, taken, path.name, f"credentials: {e}")
                processed["failed"] += 1
                continue
        if process_file(taken, bot, paths, executor=executor):
            processed["ok"] += 1
        else:
            processed["failed"] += 1
    if processed["picked"]:
        _record_exec_latency(bot, paths, round(time.time() - t0, 2),
                             failed=processed["failed"])
    return processed


def _record_exec_latency(bot: BotConfig, paths: ProjectPaths, seconds: float,
                         failed: int = 0) -> None:
    """把本批执行耗时与失败数写到 run 侧记录（health.run.json）。

    plan-loop 与 run 是两个进程，而 heartbeat() 是整体覆盖语义 —— 共用 health.json
    会互相抹掉字段，所以 run 侧单独写，由 HealthMonitor.check() 合并读。
    """
    try:
        from .monitoring import HealthMonitor

        hm = HealthMonitor(paths.root, bot.bot_id, role="run")
        hm.heartbeat(exec_latency=seconds)
        hm.record_exec_result(failed)
    except Exception:  # noqa: BLE001
        pass


def _orphan_sweep(bot: BotConfig, paths: ProjectPaths) -> int:
    """定期扫孤儿保护单（TP/SL 无对应持仓）。返回撤单数。

    为什么需要：close/reduce 动作后才清理远远不够 ——
    TP/SL 由交易所触发平仓时，对应保护单会变成孤儿一直挂着。
    """
    try:
        client = bot.create_client()
    except Exception:  # noqa: BLE001
        return 0
    executor = Executor(
        client,
        label_prefix=getattr(bot, "label_prefix", "") or bot.bot_id,
        root=paths.root,
        bot_id=bot.bot_id,
    )
    total = 0
    for sym in (bot.symbols or []):
        try:
            cleaned = executor._cleanup_orphan_protectors(sym)
            if cleaned:
                total += len(cleaned)
                log.info("orphan sweep %s %s: cancelled %s", bot.bot_id, sym, cleaned)
        except Exception:  # noqa: BLE001
            continue
    return total


_AUTO_PROTECT_ALERT_SEC = 3600.0


def _auto_protect_sweep(bot: BotConfig, paths: ProjectPaths, alerted: dict) -> int:
    """定期给裸仓补 SL（`account_risk.auto_protect` 逐 bot 开启，默认关）。

    与 `_orphan_sweep` 是一对：一个撤孤儿、一个补缺失。但**判据故意不共用** ——
    补保护只看「持仓张数 vs owned SL 覆盖张数」（`Executor._owned_sl_size`），
    `Executor._has_pending_entry` **只归孤儿扫描**。

    曾经共用过一版（补保护也拿 `_has_pending_entry` 当闸门），结果是补保护被一个
    会静默返回 False 的判据挡住 —— 那个判据连着修了两次漏判（`62dce02` 条件单、
    `b0307ab` 空头 `left` 为负），而漏判的代价是「裸仓一直没人管」，正是这个功能
    要消灭的状态。`51264d5` 已删掉那道闸门，**别再耦合回去**。

    为什么必须在这里、而不是 plan-loop 里：裸仓就是「没人管时静静躺着」的那个状态。
    挂在 LLM 轮次上会被它的 15 分钟节奏绑架，而且 LLM 进程死了就完全不跑。

    `alerted` 是跨轮共享的限流表：同一 (bot, symbol) 的失败告警按
    `_AUTO_PROTECT_ALERT_SEC` 节流，否则每轮扫描都刷一条。
    """
    try:
        client = bot.create_client()
    except Exception:  # noqa: BLE001
        return 0
    executor = Executor(
        client,
        label_prefix=getattr(bot, "label_prefix", "") or bot.bot_id,
        root=paths.root,
        bot_id=bot.bot_id,
        account_risk=getattr(bot, "account_risk", None),
        alert_store=_alert_store(paths, bot.bot_id),
    )
    done = 0
    for sym in (bot.symbols or []):
        try:
            out = executor.ensure_protection(sym)
        except Exception as e:  # noqa: BLE001 — 单个 symbol 失败不拖垮整轮扫描
            log.warning("auto protect %s %s raised: %s", bot.bot_id, sym, e)
            continue
        if out.get("skipped") or out.get("auto_protect") == "off":
            if out.get("skipped") and out.get("auto_protect") == "dry":
                # dry 模式下必须把「为什么没动手」也记下来 —— 否则「没有日志」既可能是
                # 「已有保护」也可能是「扫描根本没跑」，dry 观察期就验证不了任何东西
                # （这正是本项目反复出现的「静默失效」形态）。
                log.info("auto protect[dry] %s %s: 跳过（%s）", bot.bot_id, sym, out["skipped"])
            continue
        if out.get("placed"):
            done += 1
            log.warning(
                "auto protect %s %s: 裸仓补 SL id=%s price=%s size=%s",
                bot.bot_id, sym, out["placed"].get("id"), out.get("sl"), out.get("position_size"),
            )
            continue
        if out.get("auto_protect") == "dry":
            log.info(
                "auto protect[dry] %s %s: 会挂 SL %s (mark=%s pct=%s side=%s size=%s)",
                bot.bot_id, sym, out.get("sl"), out.get("mark"), out.get("sl_pct"),
                out.get("position_side"), out.get("position_size"),
            )
            continue
        if out.get("error"):
            key = f"{bot.bot_id}:{sym}"
            now = time.time()
            if now - float(alerted.get(key) or 0) < _AUTO_PROTECT_ALERT_SEC:
                continue
            alerted[key] = now
            log.error("auto protect %s %s: 补保护失败 %s", bot.bot_id, sym, out["error"])
            try:
                if executor.alert_store is not None:
                    executor.alert_store.raise_alert(
                        "unprotected_position",
                        f"{sym} 裸仓补 SL 失败：{out['error']}"
                        f"（只告警、未自动平仓，需人工或下一轮 plan 处理）",
                        symbol=sym, auto_protect_error=str(out["error"]),
                    )
            except Exception:  # noqa: BLE001
                pass
    return done


def select_bots(bots: dict[str, BotConfig], only: Optional[str] = None,
                allow_disabled: bool = False) -> dict[str, BotConfig]:
    """挑出要跑执行循环的 bot。

    `enabled` 是「watchdog 该不该管」的判据，但 persona 讨论组需要一个
    **只执行、不分析**的消费者：组的成员必须保持 `enabled: false`（否则
    watchdog 会拉起它们各自的 plan-loop，与讨论组的融合单在同一账户上互相
    打架），而 target 账户的 inbox 又要有人消费。所以显式 `--bot X` +
    `allow_disabled` 时按名字跑，不看 enabled。

    没给 `only` 时 `allow_disabled` **不生效** —— 否则等于把全部 bot 都拉起来。
    """
    if only and allow_disabled:
        return {k: v for k, v in bots.items() if k == only}
    return {k: v for k, v in bots.items() if v.enabled and (only is None or k == only)}


def run_forever(bots: dict[str, BotConfig], paths: ProjectPaths, only: Optional[str] = None,
                orphan_sweep_sec: float = 300.0,
                allow_disabled: bool = False) -> None:
    paths.ensure()
    selected = select_bots(bots, only, allow_disabled)
    if not selected:
        raise SystemExit("no enabled bots to run")
    # use max poll among bots as sleep base
    interval = min((b.poll_interval_sec for b in selected.values()), default=2.0)
    log.info("watching bots: %s (orphan sweep every %.0fs)", sorted(selected), orphan_sweep_sec)
    last_sweep = 0.0
    auto_protect_alerted: dict = {}
    while True:
        now = time.time()
        do_sweep = (now - last_sweep) >= orphan_sweep_sec
        for bot in selected.values():
            try:
                stats = run_bot_once(bot, paths)
                if stats["picked"]:
                    log.info("bot %s: %s", bot.bot_id, stats)
            except Exception as e:  # noqa: BLE001 — keep loop alive
                log.exception("bot %s crashed: %s", bot.bot_id, e)
            if do_sweep:
                # 先撤孤儿、再补缺失：顺序固定，免得刚变 flat 的 symbol 被抢跑
                try:
                    _orphan_sweep(bot, paths)
                except Exception as e:  # noqa: BLE001
                    log.warning("orphan sweep %s failed: %s", bot.bot_id, e)
                try:
                    _auto_protect_sweep(bot, paths, auto_protect_alerted)
                except Exception as e:  # noqa: BLE001
                    log.warning("auto protect sweep %s failed: %s", bot.bot_id, e)
        if do_sweep:
            last_sweep = now
        time.sleep(max(0.2, interval))
