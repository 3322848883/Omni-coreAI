"""CLI: python -m gate_bot run|once|process|status"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import load_all_bots, load_bot_config
from .gate_client import GateApiError
from .watcher import ProjectPaths, process_file, run_bot_once, run_forever


def _root_from_args(args) -> Path:
    """Project root: --root > GATE_BOT_ROOT > cwd. Works under systemd/cron without cd."""
    if getattr(args, "root", None):
        return Path(args.root).expanduser().resolve()
    env = os.environ.get("GATE_BOT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def _build_plan_runner(bot, paths: ProjectPaths):
    from .strategist.llm_client import LLMClient, LLMConfig
    from .strategist.loop import PlanRunner, StrategistConfig
    from .strategist.market import MarketConfig
    from .strategist.risk import RiskConfig

    s = dict(bot.strategist or {})
    risk = dict(s.get("risk") or {})
    llm = dict(s.get("llm") or {})
    mk = dict(s.get("market") or {})
    cfg = StrategistConfig(
        enabled=bool(s.get("enabled", True)),
        interval_sec=int(s.get("interval_sec") or 300),
        timeframe=str(s.get("timeframe") or "15m"),
        vision=bool(s.get("vision", True)),
        vision_timeframes=[str(x) for x in (s.get("vision_timeframes") or [])] or None,
        event_on_kline_close=bool(s.get("event_on_kline_close", True)),
        event_timeframe=s.get("event_timeframe") or s.get("timeframe") or "15m",
        conditions=list(s.get("conditions") or []),
        check_interval_sec=float(s.get("check_interval_sec") or 1.0),
        ai_triggers=dict(s.get("ai_triggers") or {}),
        tools=dict(s.get("tools") or {}),
        symbols=[str(x) for x in (s.get("symbols") or bot.symbols or [])],
        prompt_file=str(s.get("prompt_file") or "prompts/vergex_default.md"),
        write_hold=bool(s.get("write_hold", True)),
        candles=int(s.get("candles") or 60),
        market=MarketConfig(
            mode=str(mk.get("mode") or "rest_only"),
            pa_data_root=mk.get("pa_data_root"),
            db=str(mk.get("db") or "kline.db"),
            exchange=str(mk.get("exchange") or bot.exchange or "gate").strip().lower(),
            stale_factor=2.0 if mk.get("stale_factor") is None else float(mk.get("stale_factor")),
            health_url=mk.get("health_url"),
            indicators=[str(x) for x in (mk.get("indicators") or ["ema20", "ema50", "atr14", "rsi14"])],
            extra_timeframes=(
                list(mk.get("timeframes") or mk.get("extra_timeframes") or [])
            ),
            extra_candles=int(mk.get("extra_candles") or 20),
            refresh=[str(x) for x in (mk.get("refresh") or ["ticker", "stats", "orderbook"])],
        ),
        env=bot.env,
        bot_root=paths.root,
        bot_id=bot.bot_id,
        skills=(s.get("skills") if s.get("skills") is not None else getattr(bot, "skills", None)),
        risk=RiskConfig(
            min_confidence=float(risk.get("min_confidence") or 0.75),
            max_notional_usd=float(risk["max_notional_usd"]) if risk.get("max_notional_usd") is not None else bot.max_notional_usd,
            max_chips=int(risk.get("max_chips") or 3),
            allow_actions=set(risk["allow_actions"]) if risk.get("allow_actions") else None,
        ),
        llm=LLMConfig(),
    )
    from .providers import resolve_llm_config

    cfg.llm = resolve_llm_config(paths.root, bot_id=bot.bot_id, llm=llm)
    client = bot.create_client()
    history = paths.bot_paths(bot.bot_id).state
    return PlanRunner(client, cfg, paths.bot_inbox(bot.bot_id), history, llm=LLMClient(cfg.llm))


def cmd_plan(args) -> int:
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    bot = load_bot_config(paths.config_dir / f"{args.bot}.yaml")
    runner = _build_plan_runner(bot, paths)
    result = runner.run_once()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def cmd_plan_loop(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    from .pidlock import PidLock

    # worker holds the lock itself — survives supervisor death (orphan-safe)
    lock = PidLock(paths.bot_paths(args.bot).lock_plan).acquire()
    if lock is None:
        print(f"plan-loop already running for {args.bot}", flush=True)
        return 3
    bot = load_bot_config(paths.config_dir / f"{args.bot}.yaml")
    runner = _build_plan_runner(bot, paths)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("bye")
    finally:
        if lock is not None:
            lock.release()
    return 0


def cmd_paper_score(args) -> int:
    from .metrics.scoreboard import build_scoreboard, format_table, write_scoreboard

    root = _root_from_args(args)
    board = build_scoreboard(root, run_trial_scan=not getattr(args, "no_scan", False))
    path = write_scoreboard(root, board)
    if getattr(args, "json", False):
        print(json.dumps(board, ensure_ascii=False, indent=2))
    else:
        print(format_table(board))
        print(f"\nwritten: {path}")
    return 0


def cmd_trades(args) -> int:
    from .tradelog import TradeLogger, trade_log_path

    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    log = TradeLogger(trade_log_path(paths.root, args.bot))
    rows = log.tail(args.tail)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


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
    try:
        from .ledger import Ledger, default_ledger_path

        led = Ledger(default_ledger_path(paths.root))
        out["heartbeats"] = led.heartbeats()
        led.close()
    except Exception as e:  # noqa: BLE001
        out["heartbeats"] = []
        out["ledger_error"] = str(e)
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



def cmd_paper_run(args) -> int:
    """独立 paper bot 进程：复用 run_forever 信号链 + 撮合/强平/费率 tick 线程。"""
    import threading
    import time as _time

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    from .pidlock import PidLock

    lock_path = paths.bot_paths(args.bot).lock_run
    lock = PidLock(lock_path).acquire()
    if lock is None:
        print(f"paper-run already running ({args.bot})", flush=True)
        return 3
    bots = load_all_bots(paths.config_dir)
    bot = bots.get(args.bot)
    if bot is None:
        print(f"bot not found: {args.bot}")
        return 2
    if (bot.env or "").lower() != "paper":
        print(f"bot {args.bot} env={bot.env} 不是 paper，用 run 命令")
        return 2

    client = bot.create_client()
    tick_stop = threading.Event()

    def _tick_loop():
        interval = float((bot.paper or {}).get("tick_interval_sec") or 2.0)
        while not tick_stop.is_set():
            try:
                info = client.tick()
                if info.get("liquidations"):
                    logging.warning("paper liquidation: %s", info["liquidations"])
            except Exception as e:  # noqa: BLE001
                logging.error("paper tick error: %s", e)
            tick_stop.wait(interval)

    th = threading.Thread(target=_tick_loop, daemon=True, name="paper-tick")
    th.start()
    logging.info("paper-run started bot=%s feed=%s", args.bot,
                 (bot.paper or {}).get("feed_exchange") or bot.exchange)
    try:
        run_forever(bots, paths, only=args.bot)
    except KeyboardInterrupt:
        print("bye")
    finally:
        tick_stop.set()
        if lock is not None:
            lock.release()
    return 0



def cmd_broadcast(args) -> int:
    """广播：一信号 → 多 bot inbox（目标来自 config/broadcast.yaml）。"""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    from .broadcast import BroadcastError, load_broadcast_config, validate_routes, Broadcaster
    from .config import load_all_bots

    cfg_path = Path(args.config) if args.config else (paths.root / "config" / "broadcast.yaml")
    try:
        cfg = load_broadcast_config(cfg_path)
    except BroadcastError as e:
        print(f"broadcast config error: {e}")
        return 2
    known = set(load_all_bots(paths.config_dir).keys())
    try:
        validate_routes(cfg, known)
    except BroadcastError as e:
        print(f"broadcast route error: {e}")
        return 2
    bc = Broadcaster(paths.root, cfg, known)
    if args.once:
        s = bc.run_once()
        print(json.dumps({k: v for k, v in s.items() if k != "records"}, ensure_ascii=False))
        for rec in s.get("records") or []:
            if rec.get("failed"):
                print(f"  FAILED {rec.get('route')}/{rec.get('file')}: {rec.get('failed')}")
        # partial 也算失败（部分目标没收到，不可静默成功）
        return 0 if (s.get("failed", 0) == 0 and s.get("partial", 0) == 0) else 1
    try:
        bc.run_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


def cmd_persona_run(args) -> int:
    """多人格共管：触发 → 各人格独立分析 → 融合 → 执行。"""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    from .pidlock import PidLock
    from .config import load_all_bots
    from .persona import PersonaError, load_persona_groups, validate_group, PersonaRunner
    from .strategist.loop import PlanRunner
    from .strategist.loop import StrategistConfig  # noqa: F401
    from . import __main__ as _m

    cfg_path = Path(args.config) if args.config else (paths.root / "config" / "persona_groups.yaml")
    try:
        groups = load_persona_groups(cfg_path)
    except PersonaError as e:
        print(f"persona config error: {e}")
        return 2
    known = set(load_all_bots(paths.config_dir).keys())
    target_groups = [g for g in groups if g.enabled and (not args.group or g.name == args.group)]
    if not target_groups:
        print(f"no enabled group matched: {args.group or '<all>'}")
        return 2
    for g in target_groups:
        try:
            validate_group(g, known)
        except PersonaError as e:
            print(f"persona group error: {e}")
            return 2

    # 组内每个成员建 PlanRunner（独立分析器）
    bots = load_all_bots(paths.config_dir)
    runners = {}
    for g in target_groups:
        for bot_id in g.members:
            if bot_id in runners:
                continue
            bot = bots[bot_id]
            try:
                runners[bot_id] = _m._build_plan_runner(bot, paths)
            except Exception as e:  # noqa: BLE001
                print(f"build runner {bot_id} failed: {e}")
                return 2

    lock = None
    if args.group:
        lock = PidLock(paths.root / "data" / "shared" / f"persona-{args.group}.lock").acquire()
        if lock is None:
            print(f"persona-run already running ({args.group})")
            return 3

    try:
        if args.once:
            outs = []
            for g in target_groups:
                r = PersonaRunner(paths.root, g, bots, runners)
                res = r.run_once(trigger="manual")
                outs.append({k: v for k, v in res.items() if k != "plans"})
            print(json.dumps(outs, ensure_ascii=False, default=str))
            return 0 if all(o.get("ok") for o in outs) else 1
        import time as _t
        print(f"persona-run start: {[g.name for g in target_groups]}")
        while True:
            for g in target_groups:
                try:
                    r = PersonaRunner(paths.root, g, bots, runners)
                    res = r.run_once(trigger="interval")
                    print(f"{_t.strftime('%H:%M:%S')} {g.name}: {res.get('decision')} "
                          f"executed={res.get('executed')} order={res.get('order_id')}")
                except Exception as e:  # noqa: BLE001
                    print(f"{_t.strftime('%H:%M:%S')} {g.name} error: {e}")
            _t.sleep(int(args.interval) if args.interval else 300)
    except KeyboardInterrupt:
        print("bye")
    finally:
        if lock is not None:
            lock.release()
    return 0

def cmd_run(args) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    paths = ProjectPaths(_root_from_args(args))
    paths.ensure()
    from .pidlock import PidLock

    # same lock path as supervisor: bot_paths().lock_run when --bot is set
    if args.bot:
        lock_path = paths.bot_paths(args.bot).lock_run
        lock_name = f"{args.bot}/state/run.lock"
    else:
        lock_path = paths.root / "data" / "bots" / "_all.run.lock"
        lock_name = "_all.run.lock"
    lock = PidLock(lock_path).acquire()
    if lock is None:
        print(f"run already running ({lock_name})", flush=True)
        return 3
    bots = load_all_bots(paths.config_dir)
    try:
        run_forever(bots, paths, only=args.bot)
    except KeyboardInterrupt:
        print("bye")
    finally:
        if lock is not None:
            lock.release()
    return 0


def cmd_supervisor(args) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from .supervisor import Supervisor

    sup = Supervisor(
        _root_from_args(args),
        bot_ids=args.bot,
        auto_migrate=bool(args.auto_migrate),
        check_interval_sec=float(args.check_interval or 2.0),
    )
    try:
        sup.run_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


def cmd_deploy_check(args) -> int:
    """部署后验证：代码版本 / overlay / 启用 bot / skill / 健康。"""
    import json as _json
    import subprocess

    root = _root_from_args(args)
    paths = ProjectPaths(root)
    ok = True

    print("=" * 52)
    print("部署检查")
    print("=" * 52)

    # 1) 代码版本
    def _git(*a: str) -> str:
        try:
            return subprocess.run(
                ["git", *a], cwd=str(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    try:
        head = _git("log", "--oneline", "-1")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = _git("status", "--porcelain")
        print(f"代码版本:   {branch} @ {head}")
        if dirty:
            n = len(dirty.splitlines())
            print(f"工作区:     {n} 个未提交改动")
    except Exception as e:  # noqa: BLE001
        print(f"代码版本:   (git 不可用: {e})")

    # 2) overlay
    from .config import load_all_bots, overlay_dir_for

    ov_dir = overlay_dir_for(paths.config_dir)
    ov_files = sorted(ov_dir.glob("*.yaml")) if ov_dir.is_dir() else []
    print(f"配置 overlay: {len(ov_files)} 个 ({ov_dir})")

    # 3) 启用 bot
    try:
        bots = load_all_bots(paths.config_dir)
        enabled = sorted(k for k, v in bots.items() if v.enabled)
        print(f"启用 bot:   {len(enabled)} 个 {enabled if len(enabled) <= 8 else enabled[:8] + ['...']}")
    except Exception as e:  # noqa: BLE001
        print(f"启用 bot:   ⛔ 配置加载失败: {e}")
        ok = False

    # 3b) 基线违规检测（基线写 enabled: true 会被忽略，属误改）
    violations = []
    for p in sorted(paths.config_dir.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        try:
            import re as _re

            txt = p.read_text(encoding="utf-8")
            if _re.search(r"^enabled:\s*true\s*$", txt, _re.MULTILINE):
                violations.append(p.name)
        except Exception:  # noqa: BLE001
            continue
    if violations:
        print(f"⚠️  基线违规: {len(violations)} 个基线文件写了 enabled: true（已忽略，应改 overlay）")
        for v in violations[:5]:
            print(f"              config/bots/{v}")
        ok = False
    else:
        print("基线检查:   OK（无基线 enabled: true）")

    # 4) skill
    try:
        from .skillkit import SkillRegistry
        from .skillkit.loader import BUNDLED_DIRS

        reg = SkillRegistry()
        reg.scan([root / ".mimocode" / "skills", root / "skills"])
        ids = reg.ids()
        print(f"已装 skill: {len(ids)} 个 {ids}")
        for sid in ids:
            pkg = reg.get_package(sid)
            print(f"              {sid} v{pkg.meta.version or '-'} ({len(pkg.files)} files)")
    except Exception as e:  # noqa: BLE001
        print(f"已装 skill: ⛔ {e}")
        ok = False

    # 5) 健康
    health_dir = root / "data" / "bots"
    if health_dir.is_dir():
        for hf in sorted(health_dir.glob("*/state/health.json")):
            try:
                h = _json.loads(hf.read_text(encoding="utf-8"))
                bot = hf.parent.parent.name
                streak = h.get("error_streak", 0)
                mark = "OK" if streak == 0 else f"⚠️ error_streak={streak}"
                print(f"健康 {bot}: {mark}  cycle={h.get('cycle_id', '-')}")
                if streak:
                    ok = False
            except Exception:  # noqa: BLE001
                continue

    print("=" * 52)
    print("结论:", "PASS" if ok else "有告警")
    return 0 if ok else 1


def cmd_watchdog(args) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .watchdog import Watchdog

    wd = Watchdog(
        _root_from_args(args),
        bot_ids=args.bot,
        interval_sec=float(args.interval or 15.0),
        max_restarts_per_hour=int(args.max_restarts or 5),
        notify=not args.no_notify,
    )
    try:
        wd.run_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


def cmd_backtest(args) -> int:
    from .backtest import BacktestReplayer

    root = _root_from_args(args)
    rp = BacktestReplayer(root, args.bot)
    closes = None
    if getattr(args, "closes", None):
        try:
            closes = [float(x) for x in args.closes.split(",") if x.strip()]
        except ValueError:
            print("bad --closes, expected comma-separated floats", file=sys.stderr)
            return 2
    result = rp.replay(days=int(args.days), closes=closes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 2


def cmd_migrate(args) -> int:
    root = _root_from_args(args)
    from .migrate import migrate_all, migrate_bot

    if args.bot:
        reports = [migrate_bot(root, args.bot, dry_run=bool(args.dry_run))]
    else:
        reports = migrate_all(root, dry_run=bool(args.dry_run))
    print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gate_bot", description="Gate strategy signal bot")
    parser.add_argument("--root", default=None, help="project root (default cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="watch inbox forever")
    p_run.add_argument("--bot", default=None)
    p_run.set_defaults(func=cmd_run)
    p_prun = sub.add_parser("paper-run", help="run one paper bot (local simulated exchange)")
    p_prun.add_argument("--bot", required=True)
    p_prun.add_argument("--root", default="")
    p_prun.set_defaults(func=cmd_paper_run)
    p_persona = sub.add_parser("persona-run", help="multi-persona co-managed orders (fuse then execute)")
    p_persona.add_argument("--group", default="", help="persona group name (default: all enabled)")
    p_persona.add_argument("--config", default="", help="persona_groups.yaml path")
    p_persona.add_argument("--interval", default="", help="loop interval sec (default 300)")
    p_persona.add_argument("--once", action="store_true", help="one cycle then exit")
    p_persona.add_argument("--root", default="")
    p_persona.set_defaults(func=cmd_persona_run)

    p_once = sub.add_parser("once", help="one scan pass then exit")
    p_once.add_argument("--bot", default=None)
    p_once.set_defaults(func=cmd_once)

    p_proc = sub.add_parser("process", help="process a single JSON file")
    p_proc.add_argument("file")
    p_proc.add_argument("--bot", required=True)
    p_proc.set_defaults(func=cmd_process)

    p_tr = sub.add_parser("trades", help="print trade journal (JSONL tail)")
    p_tr.add_argument("--bot", required=True)
    p_tr.add_argument("--tail", type=int, default=50)
    p_tr.set_defaults(func=cmd_trades)

    p_sc = sub.add_parser("paper-score", help="strategy scoreboard (metrics + trials)")
    p_sc.add_argument("--json", action="store_true", help="print full JSON")
    p_sc.add_argument("--no-scan", action="store_true", help="skip trial file scan")
    p_sc.set_defaults(func=cmd_paper_score)

    p_st = sub.add_parser("status", help="show bots and backlog")
    p_st.set_defaults(func=cmd_status)

    p_plan = sub.add_parser("plan", help="one LLM plan cycle → write inbox")
    p_plan.add_argument("--bot", required=True)
    p_plan.set_defaults(func=cmd_plan)

    p_pl = sub.add_parser("plan-loop", help="LLM strategist loop (interval + kline close)")
    p_pl.add_argument("--bot", required=True)
    p_pl.set_defaults(func=cmd_plan_loop)

    p_sup = sub.add_parser("supervisor", help="multi-bot process supervisor (plan-loop+run)")
    p_sup.add_argument("--bot", action="append", default=None, help="limit to bot id(s)")
    p_sup.add_argument("--auto-migrate", action="store_true")
    p_sup.add_argument("--check-interval", type=float, default=2.0)
    p_sup.set_defaults(func=cmd_supervisor)

    p_mig = sub.add_parser("migrate", help="legacy layout → data/bots/<id> + import jsonl")
    p_mig.add_argument("--bot", default=None)
    p_mig.add_argument("--dry-run", action="store_true")
    p_mig.set_defaults(func=cmd_migrate)
    p_bc = sub.add_parser("broadcast", help="fan out signals to multiple bot inboxes (config-driven)")
    p_bc.add_argument("--config", default="", help="broadcast.yaml path (default config/broadcast.yaml)")
    p_bc.add_argument("--once", action="store_true", help="one scan pass then exit")
    p_bc.add_argument("--root", default="")
    p_bc.set_defaults(func=cmd_broadcast)

    p_bt = sub.add_parser("backtest", help="journal replay backtest (PnL/Sharpe/DSR)")
    p_bt.add_argument("--bot", required=True)
    p_bt.add_argument("--days", type=int, default=30)
    p_bt.add_argument("--closes", default="", help="comma-separated closes for benchmark")
    p_bt.set_defaults(func=cmd_backtest)

    p_wd = sub.add_parser("watchdog", help="auto-restart dead bot processes + notify")
    p_wd.add_argument("--bot", action="append", default=None, help="limit to bot id(s)")
    p_wd.add_argument("--interval", type=float, default=15.0, help="check interval seconds")
    p_wd.add_argument("--max-restarts", type=int, default=5, help="max restarts per hour per component")
    p_wd.add_argument("--no-notify", action="store_true", help="disable Feishu/Telegram push")
    p_wd.set_defaults(func=cmd_watchdog)

    from .skillkit.cli import register_parser as _register_skill_parser
    _register_skill_parser(sub)

    p_dc = sub.add_parser("deploy-check", help="部署后验证（版本/overlay/bot/skill/健康）")
    p_dc.add_argument("--root", help="project root (default: cwd / GATE_BOT_ROOT)")
    p_dc.set_defaults(func=cmd_deploy_check)
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
