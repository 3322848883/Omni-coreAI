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


# ── 守护扫描集合：`bot.symbols` ∪ 「交易所上本 bot 有归属痕迹的合约」─────
#
# 为什么必须扩展（设计 S2.4⑦ / D6）：5 个 sweep 原先一律 `for sym in bot.symbols`
# —— 于是**从 yaml 里删掉一个币，它上面的存量仓位立刻脱离全部守护**（裸仓）。
# 而「删旧币、加新币」正是换标的的核心场景，覆盖面不该由「配置里还写不写它」决定。
#
# 为什么不能按「账户里有仓」扩展：账户是**多 bot 共用**的，按仓扩展会把别的 bot 的
# 仓位拉进本 bot 的扫描集合 —— 后果是替他 bot 补 SL / 上移 SL，属于错币操作。
# 所以归属判据取**痕迹**：订单 text 前缀 `t-<label_prefix>`（与 `executor._text_owned`
# 同一条规则，segment-safe：`t-x` 或 `t-x-*`，`t-xXX` 不算）。
# 有仓、但一个本 bot 的挂单/条件单都没有 → 判不了归属，**不扫**（宁可少扫不越界）。
#
# 币数上限是成本闸门（设计 S2.7「成本有界」）：币越多每轮 REST 调用越多，
# 无上限时扫描周期会随 N 线性拉长。超限只扫前 N 个并告警 + 落 `over_cap`。
_SCAN_SYMBOL_CAP = 12


def _text_owned(text: str, label: str) -> bool:
    """归属判据（与 `executor._text_owned` 同规则，segment-safe）。

    空 label 一律 False —— 归属判据必须 fail-closed，否则「没配 label_prefix」
    会退化成「整个账户都是我的」。
    """
    label = str(label or "").strip()
    if not label:
        return False
    text = str(text or "")
    tag = f"t-{label}"
    return text == tag or text.startswith(tag + "-")


def _order_text(row: dict) -> str:
    """订单文本：条件单在 `initial.text`，普通挂单在 `text`。"""
    init = row.get("initial") if isinstance(row.get("initial"), dict) else {}
    return str(init.get("text") or row.get("text") or "")


def _order_contract(row: dict) -> str:
    """订单合约：条件单在 `initial.contract`，普通挂单在 `contract`。"""
    init = row.get("initial") if isinstance(row.get("initial"), dict) else {}
    return str(init.get("contract") or row.get("contract") or "")


def _own_label(bot: BotConfig) -> str:
    return str(getattr(bot, "label_prefix", "") or bot.bot_id or "").strip()


def _position_size(p: dict) -> int:
    try:
        return int(p.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def guard_scan_set(bot: BotConfig, client=None) -> dict:
    """本轮守护扫描集合 + 跳过明细（设计 S2.4⑦ / D6）。

    返回 `{"symbols", "universe", "extra", "skipped", "errors", "positions"}`：

    - `symbols`   本轮真正要扫的币（`universe` 在前、归属合约殿后、受上限截断）
    - `extra`     不在 yaml 里、但本 bot 有归属痕迹的合约（**删币后仍被守护的就是它们**）
    - `skipped`   `[{"symbol", "reason"}]`，reason ∈ `over_cap`（超上限没扫）/
                  `not_owned`（账户上有仓、但本 bot 无归属痕迹且不在 yaml → 不是我的）
    - `errors`    取归属痕迹/持仓时的失败（取不到时**不扩展**，只记原因）
    - `positions` 账户里有仓的合约（供事后回答「这个仓到底有没有被守护」）

    单币且账户上没有额外归属合约时 `symbols == list(bot.symbols)`，逐字不变。
    """
    universe: list[str] = []
    for s in (getattr(bot, "symbols", None) or []):
        s = str(s or "").strip()
        if s and s not in universe:
            universe.append(s)
    extra: list[str] = []
    skipped: list[dict] = []
    errors: list[str] = []
    label = _own_label(bot)
    if client is not None and label:
        # 账户级拉一次挂单 + 条件单（各 1 个 REST 调用），按 text 前缀判归属。
        for kind, meth in (("orders", "list_orders"), ("price_orders", "list_price_orders")):
            fn = getattr(client, meth, None)
            if fn is None:
                continue
            try:
                rows = fn() or []
            except Exception as e:  # noqa: BLE001 — 取不到就不扩展，原因留痕
                errors.append(f"{kind}: {e}")
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if not _text_owned(_order_text(row), label):
                    continue
                sym = _order_contract(row)
                if sym and sym not in universe and sym not in extra:
                    extra.append(sym)

    scanned = universe + extra
    if len(scanned) > _SCAN_SYMBOL_CAP:
        for sym in scanned[_SCAN_SYMBOL_CAP:]:
            skipped.append({"symbol": sym, "reason": "over_cap"})
        scanned = scanned[:_SCAN_SYMBOL_CAP]
        log.warning("guard scan %s: 币数 %d 超过上限 %d，本轮只扫前 %d 个（其余标记 over_cap）",
                    bot.bot_id, len(universe) + len(extra), _SCAN_SYMBOL_CAP, _SCAN_SYMBOL_CAP)

    positions: list[str] = []
    if client is not None:
        try:
            rows = client.get_positions() or []
        except Exception as e:  # noqa: BLE001
            errors.append(f"positions: {e}")
            rows = []
        for p in rows:
            if not isinstance(p, dict) or _position_size(p) == 0:
                continue
            sym = str(p.get("contract") or "")
            if not sym:
                continue
            if sym not in positions:
                positions.append(sym)
            if sym not in scanned:
                # 有仓却不扫：必须能回答「为什么没守护它」
                skipped.append({"symbol": sym, "reason": "not_owned"})

    return {
        "symbols": scanned,
        "universe": universe,
        "extra": [s for s in extra if s in scanned],
        "skipped": skipped,
        "errors": errors,
        "positions": positions,
    }


def guard_coverage_path(paths: "ProjectPaths", bot_id: str) -> Path:
    """每轮扫描的覆盖记录：`data/bots/<id>/state/guard_coverage.json`。"""
    return paths.bot_paths(bot_id).state / "guard_coverage.json"


def record_guard_coverage(paths: "ProjectPaths", bot_id: str, sweep: str,
                          scan: dict) -> None:
    """把本轮扫描的 `covered`/`skipped` 落盘（原子写，失败不阻塞执行）。

    回答的是**事后**才问得出的那个问题：「这个仓到底有没有被守护」——
    只看日志回答不了（sweep 正常跑完是不打日志的），所以必须落一份可读状态。
    文件按 sweep 名分节（`round` = 300s 那五步、`give_back` = 60s 那一步），
    每节覆盖上一轮同名节，节内带 `ts` 便于判断新鲜度。
    """
    try:
        p = guard_coverage_path(paths, bot_id)
        payload: dict = {}
        if p.exists():
            try:
                payload = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 — 坏文件不该拦住写
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        rounds = payload.get("rounds")
        if not isinstance(rounds, dict):
            rounds = {}
        rounds[str(sweep)] = {
            "ts": int(time.time()),
            "universe": list(scan.get("universe") or []),
            "covered": list(scan.get("symbols") or []),
            "covered_extra": list(scan.get("extra") or []),
            "skipped": list(scan.get("skipped") or []),
            "counts": {
                "covered": len(scan.get("symbols") or []),
                "skipped": len(scan.get("skipped") or []),
                "extra": len(scan.get("extra") or []),
            },
            "positions": list(scan.get("positions") or []),
            "errors": list(scan.get("errors") or []),
        }
        payload["bot_id"] = bot_id
        payload["updated"] = int(time.time())
        payload["cap"] = _SCAN_SYMBOL_CAP
        payload["rounds"] = rounds
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.writing")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        log.warning("record guard coverage failed (%s): %s", bot_id, e)


def _sweep_symbols(bot: BotConfig, client, symbols) -> list[str]:
    """sweep 的扫描集合：显式给了就用它，否则就地算一份（含归属合约）。"""
    if symbols is not None:
        return list(symbols)
    return list(guard_scan_set(bot, client)["symbols"])


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


def _reconcile_sweep(bot: BotConfig, paths: ProjectPaths, *,
                     client=None, symbols=None) -> int:
    """保护单张数对账：同方向 tp/sl 合计 > 持仓张数时保留最新一组、撤其余。

    与 `_orphan_sweep` 的分工：那个管「**无持仓**时的孤儿」，这个管
    「**有持仓**但保护单张数超额」—— 后者是每轮重挂入场单的必然产物
    （实测 eth-disc 6 小时堆到 30 个 / 202 张 vs 4 张持仓），而
    `executor._resync_protectors` 只在平/减仓路径被调用，加仓路径没人管。

    fail-closed：取不到交易所报告、无持仓、有未成交入场单 → 都不动。
    """
    from .reconcile import reconcile_protectors

    if client is None:
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
    for sym in _sweep_symbols(bot, client, symbols):
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


def _orphan_sweep(bot: BotConfig, paths: ProjectPaths, *,
                  client=None, symbols=None) -> int:
    """定期扫孤儿保护单（TP/SL 无对应持仓）。返回撤单数。

    为什么需要：close/reduce 动作后才清理远远不够 ——
    TP/SL 由交易所触发平仓时，对应保护单会变成孤儿一直挂着。
    """
    if client is None:
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
    for sym in _sweep_symbols(bot, client, symbols):
        try:
            cleaned = executor._cleanup_orphan_protectors(sym)
            if cleaned:
                total += len(cleaned)
                log.info("orphan sweep %s %s: cancelled %s", bot.bot_id, sym, cleaned)
        except Exception:  # noqa: BLE001
            continue
    return total


_AUTO_PROTECT_ALERT_SEC = 3600.0


def _auto_protect_sweep(bot: BotConfig, paths: ProjectPaths, alerted: dict, *,
                        client=None, symbols=None) -> int:
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
    if client is None:
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
    for sym in _sweep_symbols(bot, client, symbols):
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


def _peak_trail_sweep(bot: BotConfig, paths: ProjectPaths, alerted: dict, *,
                      client=None, symbols=None) -> int:
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
    if client is None:
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
    for sym in _sweep_symbols(bot, client, symbols):
        try:
            out = executor.check_peak_trail(sym)
        except Exception as e:  # noqa: BLE001 — 单个 symbol 失败不拖垮整轮扫描
            log.warning("peak trail %s %s raised: %s", bot.bot_id, sym, e)
            continue
        if out.get("peak_trail") == "off" or out.get("skipped") == "no_position":
            continue
        # 双向持仓时 `sides` 有两条腿 —— 逐腿判、逐腿报（每腿各有自己的
        # peak / target_sl / moved）。旧版只看顶层字段，双向下等于只看一条。
        for res in (out.get("sides") or []):
            if res.get("moved"):
                done += 1
                log.warning(
                    "peak trail %s %s/%s: SL %s → %s (peak=%s mark=%s size=%s)",
                    bot.bot_id, sym, res.get("position_side"),
                    res["moved"]["from"], res["moved"]["to"],
                    res.get("peak"), res.get("mark"), res["moved"]["size"],
                )
                continue
            if out.get("peak_trail") == "dry" and res.get("target_sl") is not None \
                    and not res.get("skipped"):
                log.info(
                    "peak trail[dry] %s %s/%s: 会挂 SL %s (peak=%s mark=%s atr%%=%s x%s)",
                    bot.bot_id, sym, res.get("position_side"), res.get("target_sl"),
                    res.get("peak"), res.get("mark"), res.get("atr_pct"),
                    out.get("atr_mult"),
                )
                continue
            if not (res.get("error") or res.get("skipped") == "trail_breached"):
                continue
            key = f"{bot.bot_id}:{sym}:{res.get('position_side')}"
            now = time.time()
            if now - float(alerted.get(key) or 0) < _AUTO_PROTECT_ALERT_SEC:
                continue
            alerted[key] = now
            sname = res.get("position_side") or "?"
            if res.get("error"):
                detail = (f"{sym} {sname} 峰值回撤上移 SL 失败：{res['error']}"
                          f"（旧 SL 仍在，未裸仓）")
            else:
                detail = (f"{sym} {sname} 峰值回撤已越过目标 {res.get('target_sl')}"
                          f"（峰值 {res.get('peak')} / 现价 {res.get('mark')}）"
                          f"，未挂单 —— 需人工或下一轮 plan 决定是否落袋")
            log.error("peak trail %s %s: %s", bot.bot_id, sym, detail)
            try:
                if executor.alert_store is not None:
                    executor.alert_store.raise_alert(
                        TYPE_PEAK_TRAIL, detail, symbol=sym,
                        target_sl=res.get("target_sl"), peak=res.get("peak"),
                        mark=res.get("mark"), peak_trail_error=res.get("error"),
                    )
            except Exception:  # noqa: BLE001
                pass
    return done


def _give_back_sweep(bot: BotConfig, paths: ProjectPaths, alerted: dict, *,
                     client=None, symbols=None) -> int:
    """浮盈从峰值回撤 `give_back_pct`% → 平掉仓位（`account_risk.give_back` 逐 bot 开）。

    与 `_peak_trail_sweep` 的区别：那个**上移 SL**（防坐电梯），这个**直接平仓**
    （落袋）。阈值是**逐计划**的（模型每笔自己算），由 `Executor.remember_give_back`
    在开仓/挂单时落进 `state/give_back.json`，这里按腿取用。

    **挂在快扫描（60s）而不是 300s 的 do_sweep 上**：回撤保护对时间敏感 ——
    300s 一次意味着价格可能已经多回撤一截，落袋价与目标差得远。

    三种必须让人知道的结局（按 `_AUTO_PROTECT_ALERT_SEC` 节流）：
      - `error`        平仓失败 → 仓位仍在，SL 仍有效，不是裸仓
      - 正常平仓       记 warning 日志（含峰值/落袋价）
      - `dry`          只记 info，说明「会平在哪」
    """
    mode = (getattr(bot, "account_risk", None) or {}).get("give_back", False)
    if not mode:
        return 0
    dry = str(mode).lower() == "dry"
    if client is None:
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
    for sym in _sweep_symbols(bot, client, symbols):
        try:
            out = executor.check_give_back(sym, dry=dry)
        except Exception as e:  # noqa: BLE001 — 单个 symbol 失败不拖垮整轮扫描
            log.warning("give back %s %s raised: %s", bot.bot_id, sym, e)
            continue
        for res in (out.get("sides") or []):
            if res.get("closed"):
                done += 1
                log.warning(
                    "give back %s %s/%s: 平仓 pnl=%.4f (peak=%.4f size=%s)",
                    bot.bot_id, sym, res.get("position_side"),
                    float(res["closed"].get("pnl") or 0),
                    float(res["closed"].get("peak_pnl") or 0),
                    res["closed"].get("size"),
                )
                continue
            if dry and res.get("skipped") == "dry":
                log.info(
                    "give back[dry] %s %s/%s: 会平仓 pnl=%.4f (peak=%.4f trigger=%.4f 1R=%.4f)",
                    bot.bot_id, sym, res.get("position_side"),
                    float(res.get("cur_pnl") or 0), float(res.get("peak_pnl") or 0),
                    float(res.get("trigger_pnl") or 0), float(res.get("r_usd") or 0),
                )
                continue
            if not res.get("error"):
                continue
            key = f"{bot.bot_id}:{sym}:{res.get('position_side')}:giveback"
            now = time.time()
            if now - float(alerted.get(key) or 0) < _AUTO_PROTECT_ALERT_SEC:
                continue
            alerted[key] = now
            log.error("give back %s %s: %s", bot.bot_id, sym, res["error"])
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


def _steps_from_closed(closed: list) -> list:
    """把投影检测到的平仓明细转成 `format_trade_card` 认得的 step 形状。

    **为什么需要**：SL/TP 触发是**交易所侧**成交，没有 executor 的 `steps` ——
    而 `monitoring/notify.py::format_trade_card` 正是从 `steps` 生成卡片的。
    于是「改单、开仓有通知，止盈/止损触发没有」：前者是本地发起（有 steps），
    后者不是。实测 2026-10-07 用户反馈「没有止盈通知」。

    转成 `action="close"` 就会落进 `_CLOSE_ACTIONS` 分支，由 `detail.realized_pnl`
    的正负自动判成「止盈 / 止损 / 平仓」，与本地平仓的卡片完全同款。
    """
    out = []
    for f in (closed or []):
        if not isinstance(f, dict):
            continue
        out.append({
            "action": "close",
            "symbol": str(f.get("contract") or ""),
            "ok": True,
            # 时间放在 **step 层**：`notify._step_time` 依次读
            # `detail.order.create_time` → `detail.create_time` → `s.create_time`
            # → **`s.ts`**，放进 `detail.ts` 读不到，会 fallback 成「当前时间」
            # （实测卡片上显示的是渲染时刻，不是平仓时刻）。
            "ts": f.get("time"),
            "detail": {
                "realized_pnl": f.get("pnl"),
                "entry_price": f.get("entry_price"),
                "size": f.get("size"),
                # 标明来源，便于日后区分「交易所侧触发」与「本地主动平仓」
                "trigger_source": "exchange_position_close",
            },
        })
    return out


def _exchange_pnl_sweep(bot: BotConfig, paths: ProjectPaths) -> int:
    """把交易所成交里的已实现盈亏投影进画像（`state/exchange_pnl.json`）。

    **为什么挂在 `run` 的 300s sweep 上、而不是 LLM 轮次上**：SL/TP 触发是交易所侧
    成交，不等任何人；而画像的读侧（`MemoryProfile.ledger_stats`）在 plan-loop 进程
    里每轮都读 —— 写侧只要保证「最终会写到」即可，不必与决策同频。

    只对 **live** 跑：paper 的平仓本来就在本地账本里，`ledger_stats` 直接读它。

    按 `bot.symbols` 做**统计白名单**：请求拉的是全账户（游标是全账户的 ——
    `position_close` 没有单调 id，只能用时间戳，而时间戳无法按 symbol 分段；
    按 symbol 分别请求会让同一个 cursor 被反复覆写），但只有本 bot 的 symbol 计入
    画像。实测该账户的平仓历史里混着 `ETH_USDT`，全计入会让 brooks-btc 的
    「历史表现」失真。
    """
    if str(getattr(bot, "env", "") or "") != "live":
        return 0
    try:
        client = bot.create_client()
    except Exception as e:  # noqa: BLE001
        log.warning("exchange pnl sync %s: no client (%s)", bot.bot_id, e)
        return 0
    from .memory.exchange_pnl import sync as _sync

    try:
        res = _sync(paths.root, bot.bot_id, client,
                    contracts=list(bot.symbols or []))
    except Exception as e:  # noqa: BLE001
        log.warning("exchange pnl sync %s failed: %s", bot.bot_id, e)
        return 0
    if not res.get("ok"):
        log.warning("exchange pnl sync %s: %s", bot.bot_id, res.get("error"))
        return 0
    added = int(res.get("added") or 0)
    if added:
        log.info("exchange pnl %s: +%d 笔平仓（累计 %d 笔）",
                 bot.bot_id, added, res.get("trades"))

    # ── 补发「止盈/止损触发」通知 ──
    # 这类平仓没有 executor 的 steps，`run` 的成交卡片路径看不到它们。
    closed = res.get("closed") or []
    if closed:
        try:
            from .monitoring import notify_trade_events
            notify_trade_events(bot.bot_id, _steps_from_closed(closed),
                                root=paths.root,
                                env=str(getattr(bot, "env", "") or "live"))
            log.info("exchange pnl %s: 补发 %d 条平仓通知", bot.bot_id, len(closed))
        except Exception as e:  # noqa: BLE001
            log.warning("exchange pnl notify %s failed: %s", bot.bot_id, e)
    return added


def _guard_round(bot: BotConfig):
    """本轮扫描的**共享 client** + 扫描集合（算一次，5 个 sweep 复用）。

    原先每个 sweep 各建一个 client、各自按币查持仓/挂单 —— REST 调用数按
    「sweep 数 × 币数」涨，币一多扫描周期就被拉长（C-14）。现在每轮 1 个 client、
    1 次扫描集合计算（含 1 次账户级挂单/条件单快照 + 1 次持仓快照）。

    建不出 client 时仍返回 `(None, scan)` —— sweep 自己会再试一次，coverage 也照样
    落盘（「这轮没扫成」本身就是要留痕的事实）。
    """
    client = None
    try:
        client = bot.create_client()
    except Exception as e:  # noqa: BLE001
        log.warning("guard scan %s: no client (%s)", bot.bot_id, e)
    return client, guard_scan_set(bot, client)


def run_forever(bots: dict[str, BotConfig], paths: ProjectPaths, only: Optional[str] = None,
                orphan_sweep_sec: float = 300.0,
                pnl_sweep_sec: float = 60.0,
                allow_disabled: bool = False) -> None:
    paths.ensure()
    selected = select_bots(bots, only, allow_disabled)
    if not selected:
        raise SystemExit("no enabled bots to run")
    # use max poll among bots as sleep base
    interval = min((b.poll_interval_sec for b in selected.values()), default=2.0)
    log.info("watching bots: %s (orphan sweep every %.0fs, pnl sweep every %.0fs)",
             sorted(selected), orphan_sweep_sec, pnl_sweep_sec)
    last_sweep = 0.0
    last_pnl = 0.0
    auto_protect_alerted: dict = {}
    peak_trail_alerted: dict = {}
    give_back_alerted: dict = {}
    while True:
        now = time.time()
        do_sweep = (now - last_sweep) >= orphan_sweep_sec
        # **平仓检测单独一个节奏**：它只读交易所的平仓历史，与保护单对账毫无关系。
        # 挂在 300s 上意味着「止盈触发了，最多 5 分钟后才通知」——而止盈是用户最想
        # 立刻知道的成交。1 分钟一次 `position_close` 请求，每天 1440 次，成本可忽略。
        do_pnl = (now - last_pnl) >= pnl_sweep_sec
        for bot in selected.values():
            try:
                stats = run_bot_once(bot, paths)
                if stats["picked"]:
                    log.info("bot %s: %s", bot.bot_id, stats)
            except Exception as e:  # noqa: BLE001 — keep loop alive
                log.exception("bot %s crashed: %s", bot.bot_id, e)
            if do_pnl:
                try:
                    _exchange_pnl_sweep(bot, paths)
                except Exception as e:  # noqa: BLE001
                    log.warning("exchange pnl sweep %s failed: %s", bot.bot_id, e)
                # 浮盈回撤平仓放**快扫描**：回撤保护对时间敏感，300s 一次太慢。
                # 只在开关打开时才去算扫描集合 —— 否则白付一次账户快照的 REST。
                if (getattr(bot, "account_risk", None) or {}).get("give_back", False):
                    client, scan = _guard_round(bot)
                    try:
                        _give_back_sweep(bot, paths, give_back_alerted,
                                         client=client, symbols=scan["symbols"])
                    except Exception as e:  # noqa: BLE001
                        log.warning("give back sweep %s failed: %s", bot.bot_id, e)
                    record_guard_coverage(paths, bot.bot_id, "give_back", scan)
            if do_sweep:
                # 顺序固定：**先对齐张数（有持仓）→ 再撤孤儿（无持仓）→ 补缺失（裸仓）
                # → 最后上移已有保护（防坐电梯）**。
                # 上移放最后：它读的是「现有 SL 在哪」，前面三步刚把 SL 集合收拾干净，
                # 这时候的目标价才是稳的。
                client, scan = _guard_round(bot)
                syms = scan["symbols"]
                try:
                    _reconcile_sweep(bot, paths, client=client, symbols=syms)
                except Exception as e:  # noqa: BLE001
                    log.warning("reconcile sweep %s failed: %s", bot.bot_id, e)
                try:
                    _orphan_sweep(bot, paths, client=client, symbols=syms)
                except Exception as e:  # noqa: BLE001
                    log.warning("orphan sweep %s failed: %s", bot.bot_id, e)
                try:
                    _auto_protect_sweep(bot, paths, auto_protect_alerted,
                                        client=client, symbols=syms)
                except Exception as e:  # noqa: BLE001
                    log.warning("auto protect sweep %s failed: %s", bot.bot_id, e)
                try:
                    _peak_trail_sweep(bot, paths, peak_trail_alerted,
                                      client=client, symbols=syms)
                except Exception as e:  # noqa: BLE001
                    log.warning("peak trail sweep %s failed: %s", bot.bot_id, e)
                # 覆盖记录落在一轮**结束**时：这样它描述的是「这一轮实际扫了什么」，
                # 而不是「每个 sweep 各自以为要扫什么」。
                record_guard_coverage(paths, bot.bot_id, "round", scan)
        if do_sweep:
            last_sweep = now
        if do_pnl:
            last_pnl = now
        time.sleep(max(0.2, interval))
