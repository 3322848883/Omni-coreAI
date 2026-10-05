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
from .monitoring.alerts import TYPE_PEAK_TRAIL
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


def receipt_path(root: Path, key: str) -> Path:
    """执行回执路径：`data/shared/receipts/<safe-key>.json`。

    回执是**执行结果的正规回传通道**。原先 persona 侧只能异步扫目标账户的
    `trades.jsonl` 尾 500 行来猜「这笔单成交了没、赚了多少」—— 取不到就跳过
    记账（实测漏记 458/459 笔）。有了回执，产出侧能直接读到结构化结果。
    """
    import re as _re

    safe = _re.sub(r"[^0-9A-Za-z_.-]", "_", str(key or ""))[:120] or "unknown"
    return Path(root) / "data" / "shared" / "receipts" / f"{safe}.json"


def write_receipt(root: Path, key: str, payload: dict) -> None:
    """原子写回执（写失败不影响主流程）。"""
    try:
        p = receipt_path(root, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.writing")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        log.warning("write receipt failed (%s): %s", key, e)


def _signal_key(data: dict, name: str) -> str:
    """信号的幂等键。

    优先用 `meta` 里的 (order_id, cycle) —— 那标识「同一张单的同一轮决策」；
    缺了就退回文件名（文件名本身带时间戳 + pid + ns，天然唯一）。
    """
    meta = data.get("meta") if isinstance(data, dict) else None
    meta = meta or {}
    oid = str(meta.get("order_id") or "")
    cyc = str(meta.get("plan_cycle") or meta.get("cycle_id") or "")
    if oid and cyc:
        return f"{oid}|{cyc}"
    return name


def _idem_file(paths: ProjectPaths, bot_id: str) -> Path:
    return paths.bot_paths(bot_id).state / "executed_signals.jsonl"


def _already_executed(paths: ProjectPaths, bot_id: str, key: str) -> bool:
    """该幂等键是否**已成功执行过**（只认 ok=True 的记录 —— 失败的允许重试）。"""
    p = _idem_file(paths, bot_id)
    if not p.exists():
        return False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()[-5000:]
    except Exception:  # noqa: BLE001
        return False
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if str(rec.get("key") or "") == key and rec.get("ok"):
            return True
    return False


def _mark_executed(paths: ProjectPaths, bot_id: str, key: str, ok: bool) -> None:
    try:
        p = _idem_file(paths, bot_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "key": key, "ok": bool(ok)}) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _archive_duplicate(paths: ProjectPaths, bot_id: str, tmp: Path,
                       original_name: str, key: str, data: object = None) -> None:
    """已执行过的信号 → archive/duplicate/（既不算成功也不算失败）。"""
    dest = paths.bot_done(bot_id).parent / "duplicate" / original_name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            os.replace(tmp, dest)
    except OSError as e:  # noqa: BLE001
        log.error("archive duplicate failed for %s: %s", original_name, e)
    log.warning("duplicate %s/%s (key=%s) — skipped", bot_id, original_name, key)


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

    # ── 执行层幂等 ──
    # 文件层的 `.taking` 重命名只能防「两个进程同时取件」，防不住「同一信号被
    # 重复投递」（复制文件、上游重发、崩溃后重放）—— 而执行层原先**没有任何
    # 幂等键**，重复投递就是重复下单。
    sig_key = _signal_key(data, original_name)
    if _already_executed(paths, bot.bot_id, sig_key):
        _archive_duplicate(paths, bot.bot_id, tmp, original_name, sig_key, data)
        return True

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
            account=getattr(bot, "account", "") or "",
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
        _mark_executed(paths, bot.bot_id, sig_key, True)
        # 回执：把执行结果写回一个约定位置，供产出侧（plan / persona）直接读，
        # 不必再去扫 trades.jsonl 猜结果。
        try:
            meta = signal.meta or {}
            steps = result.get("steps") or []
            realized = None
            entry_px = None
            for st in steps:
                det = st.get("detail") or {}
                if det.get("realized_pnl") is not None:
                    try:
                        realized = float(det["realized_pnl"])
                    except (TypeError, ValueError):
                        pass
                if entry_px is None:
                    px = det.get("entry_price") or det.get("fill_price") or det.get("avg_price")
                    try:
                        entry_px = float(px)
                    except (TypeError, ValueError):
                        pass
            write_receipt(paths.root, sig_key, {
                "key": sig_key,
                "bot_id": bot.bot_id,
                "order_id": str(meta.get("order_id") or ""),
                "cycle_id": str(meta.get("plan_cycle") or meta.get("cycle_id") or ""),
                "ts": int(time.time()),
                "ok": True,
                "realized_pnl": realized,
                "entry_price": entry_px,
                "steps": [{"action": s.get("action"), "ok": s.get("ok"),
                           "error": s.get("error")} for s in steps],
            })
        except Exception as e:  # noqa: BLE001
            log.warning("receipt build failed %s: %s", original_name, e)
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
                    account=getattr(bot, "account", "") or "",
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


def _reconcile_sweep(bot: BotConfig, paths: ProjectPaths) -> int:
    """保护单张数对账：同方向 tp/sl 合计 > 持仓张数时保留最新一组、撤其余。

    与 `_orphan_sweep` 的分工：那个管「**无持仓**时的孤儿」，这个管
    「**有持仓**但保护单张数超额」—— 后者是每轮重挂入场单的必然产物
    （实测 eth-disc 6 小时堆到 30 个 / 202 张 vs 4 张持仓），而
    `executor._resync_protectors` 只在平/减仓路径被调用，加仓路径没人管。

    fail-closed：取不到交易所报告、无持仓、有未成交入场单 → 都不动。
    """
    from .reconcile import reconcile_protectors

    try:
        client = bot.create_client()
    except Exception:  # noqa: BLE001
        return 0
    executor = Executor(
        client,
        label_prefix=getattr(bot, "label_prefix", "") or bot.bot_id,
        root=paths.root,
        bot_id=bot.bot_id,
        account=getattr(bot, "account", "") or "",
        alert_store=_alert_store(paths, bot.bot_id),
    )
    total = 0
    for sym in (bot.symbols or []):
        try:
            res = reconcile_protectors(executor, sym)
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile %s %s failed: %s", bot.bot_id, sym, e)
            continue
        n = len(res.get("cancelled") or [])
        if n:
            log.info("reconcile %s %s: 撤掉 %d 笔超额保护单（持仓 %s 张）",
                     bot.bot_id, sym, n, res.get("position_size"))
        total += n
    return total


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
        account=getattr(bot, "account", "") or "",
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
        account=getattr(bot, "account", "") or "",
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


def _peak_trail_sweep(bot: BotConfig, paths: ProjectPaths, alerted: dict) -> int:
    """单持仓峰值回撤 → 上移 SL（`account_risk.peak_trail` 逐 bot 开，默认关）。

    与 `_auto_protect_sweep` 是一对：那个补**缺失**的保护，这个**上移**已有的保护。
    两个都挂在扫描循环而不是 LLM 轮次上，理由相同 —— 浮盈回吐不等人，而且
    LLM 进程死了这条保护必须还在跑（`trail` 追踪单在本项目搁置，所以这是
    「防坐电梯」唯一的程序化手段）。

    三种必须让人知道的结局（都按 `_AUTO_PROTECT_ALERT_SEC` 节流）：
      - `error`           上移失败（挂新单没落地）→ 旧 SL 仍在，仓位不裸
      - `trail_breached`  回撤已经越过目标价 → 只报不挂（此刻挂单会立刻成交，
                          那是「市价平仓」，是另一个动作，不该由这条路径做掉）
      - 正常上移          记 warning 日志
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
        account=getattr(bot, "account", "") or "",
        account_risk=getattr(bot, "account_risk", None),
        alert_store=_alert_store(paths, bot.bot_id),
    )
    done = 0
    for sym in (bot.symbols or []):
        try:
            out = executor.check_peak_trail(sym)
        except Exception as e:  # noqa: BLE001 — 单个 symbol 失败不拖垮整轮扫描
            log.warning("peak trail %s %s raised: %s", bot.bot_id, sym, e)
            continue
        if out.get("peak_trail") == "off" or out.get("skipped") == "no_position":
            continue
        if out.get("moved"):
            done += 1
            log.warning(
                "peak trail %s %s: SL %s → %s (peak=%s mark=%s size=%s)",
                bot.bot_id, sym, out["moved"]["from"], out["moved"]["to"],
                out.get("peak"), out.get("mark"), out["moved"]["size"],
            )
            continue
        if out.get("peak_trail") == "dry" and out.get("target_sl") is not None \
                and not out.get("skipped"):
            log.info(
                "peak trail[dry] %s %s: 会挂 SL %s (peak=%s mark=%s atr%%=%s x%s)",
                bot.bot_id, sym, out.get("target_sl"), out.get("peak"),
                out.get("mark"), out.get("atr_pct"), out.get("atr_mult"),
            )
            continue
        if not (out.get("error") or out.get("skipped") == "trail_breached"):
            continue
        key = f"{bot.bot_id}:{sym}"
        now = time.time()
        if now - float(alerted.get(key) or 0) < _AUTO_PROTECT_ALERT_SEC:
            continue
        alerted[key] = now
        if out.get("error"):
            detail = f"{sym} 峰值回撤上移 SL 失败：{out['error']}（旧 SL 仍在，未裸仓）"
        else:
            detail = (f"{sym} 峰值回撤已越过目标 {out.get('target_sl')}"
                      f"（峰值 {out.get('peak')} / 现价 {out.get('mark')}）"
                      f"，未挂单 —— 需人工或下一轮 plan 决定是否落袋")
        log.error("peak trail %s %s: %s", bot.bot_id, sym, detail)
        try:
            if executor.alert_store is not None:
                executor.alert_store.raise_alert(
                    TYPE_PEAK_TRAIL, detail, symbol=sym,
                    target_sl=out.get("target_sl"), peak=out.get("peak"),
                    mark=out.get("mark"), peak_trail_error=out.get("error"),
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
    peak_trail_alerted: dict = {}
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
                # 顺序固定：**先对齐张数（有持仓）→ 再撤孤儿（无持仓）→ 补缺失（裸仓）
                # → 最后上移已有保护（防坐电梯）**。
                # 上移放最后：它读的是「现有 SL 在哪」，前面三步刚把 SL 集合收拾干净，
                # 这时候的目标价才是稳的。
                try:
                    _reconcile_sweep(bot, paths)
                except Exception as e:  # noqa: BLE001
                    log.warning("reconcile sweep %s failed: %s", bot.bot_id, e)
                try:
                    _orphan_sweep(bot, paths)
                except Exception as e:  # noqa: BLE001
                    log.warning("orphan sweep %s failed: %s", bot.bot_id, e)
                try:
                    _auto_protect_sweep(bot, paths, auto_protect_alerted)
                except Exception as e:  # noqa: BLE001
                    log.warning("auto protect sweep %s failed: %s", bot.bot_id, e)
                try:
                    _peak_trail_sweep(bot, paths, peak_trail_alerted)
                except Exception as e:  # noqa: BLE001
                    log.warning("peak trail sweep %s failed: %s", bot.bot_id, e)
        if do_sweep:
            last_sweep = now
        time.sleep(max(0.2, interval))
