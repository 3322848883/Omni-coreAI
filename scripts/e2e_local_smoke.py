# -*- coding: utf-8 -*-
"""本地端到端冒烟测试（隔离临时根，不碰真实数据）。

走**真实入口**，不是 mock：
  1. 建临时 root（config/bots + config/bots.local + data/bots/<id>/inbox）
  2. 投一个真实信号文件，用 `python -m omnialpha once` 跑（真 CLI → run_bot_once
     → process_file → execute_signal → 真 paper 交易所引擎）
  3. 校验：入场成交、SL/TP 腿挂出、**SL 张数 = 持仓全量**（不变量）、
     `trades.jsonl` 有 source=watcher 的执行记录、give_back_pct 落盘
  4. 程序层 5 条 sweep 各跑一次（真实 paper 交易所）
  5. 单 bot 闸门矩阵实测
  6. 部分覆盖：把 SL 换成只覆盖一部分 → 断言**不补差额** + 告警 + 限流

跑法：python scripts/e2e_local_smoke.py
退出码：0 = 全通过；1 = 有 FAIL
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SYM = "BTC_USDT"
BOT = "smoke-paper"
OK, BAD = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (OK if cond else BAD).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return cond


BASELINE = """\
bot_id: {bot}
enabled: false          # 基线 enabled 一律被忽略（f8b7f8b）；启用走 bots.local
env: paper
symbols: [{sym}]
label_prefix: smk
order_scope: own
require_sl: true
manage_positions: true
allow_close: true
allow_reduce: true
paper:
  feed_exchange: gate
  initial_capital: 10000
  leverage: 10
  # 刻意**不写** store_path：本脚本要验证「`--root` 本身就能隔离 paper 账户库」
  # （2026-10-14 修复 `BotConfig.root` 之前，账户会落到 CWD 并跨次叠加持仓）。
"""

SIGNAL = {
    "action": "open_long", "symbol": SYM, "size_usd": 25, "type": "market",
    "tp": 85000, "sl": 81500, "tp_mode": "trigger", "sl_mode": "trigger",
    "tp_type": "market", "sl_type": "market", "trigger_price_type": "mark",
    "give_back_pct": 30, "label": "smoke",
    "meta": {"signal_id": "smoke-1"},
}


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
    root = Path(tempfile.mkdtemp(prefix="omnialpha_smoke_"))
    try:
        # ── 1. 搭隔离 root ──
        (root / "config" / "bots").mkdir(parents=True)
        (root / "config" / "bots.local").mkdir(parents=True)
        inbox = root / "data" / "bots" / BOT / "inbox"
        inbox.mkdir(parents=True)
        (root / "config" / "bots" / f"{BOT}.yaml").write_text(
            BASELINE.format(bot=BOT, sym=SYM), encoding="utf-8")
        (root / "config" / "bots.local" / f"{BOT}.yaml").write_text(
            "enabled: true\naccount_risk:\n"
            "  auto_protect: true\n  auto_protect_sl_pct: 2.0\n", encoding="utf-8")
        (inbox / "01-long.json").write_text(
            json.dumps(SIGNAL, ensure_ascii=False, indent=2), encoding="utf-8")

        print("=" * 74)
        print("1. 真实 CLI 端到端（once）")
        print("=" * 74)
        import os
        # 刻意**不设** OMNIALPHA_ROOT、也不写 paper.store_path：唯一能把账户库
        # 放进临时根的就是 `--root`（即 `BotConfig.root`）。这样本脚本同时是
        # 「`--root` 必须隔离 paper 账户」这条修复的回归护栏。
        env = {k: v for k, v in os.environ.items() if k != "OMNIALPHA_ROOT"}
        r = subprocess.run(
            [sys.executable, "-m", "omnialpha", "--root", str(root), "once", "--bot", BOT],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300, env=env)
        print("  stdout:", (r.stdout or "").strip().replace("\n", " ")[:300])
        # CLI 的 stdout 前面有 `[PAPER:paper]` 横幅（client.banner()），
        # 所以要从第一个 `{` 开始解析，不能整体 json.loads。
        raw = r.stdout or ""
        summary = {}
        try:
            summary = json.loads(raw[raw.index("{"):])
        except Exception:  # noqa: BLE001
            pass
        st = summary.get(BOT) or {}
        if not check("信号被真实执行（ok=1）", st.get("ok") == 1, f"summary={st}"):
            print("  → 后续检查可能因无行情/无网络而失败，请先解决上一步")

        done = root / "data" / "bots" / BOT / "archive" / "done" / "01-long.json.result.json"
        steps = (json.loads(done.read_text(encoding="utf-8")).get("steps") or []) \
            if done.is_file() else []
        entry = steps[0].get("detail") if steps else {}
        entry = entry or {}
        pos_size = abs(float((entry.get("order") or {}).get("size") or 0))
        check("入场单已成交", bool((entry.get("order") or {}).get("id")),
              f"size={pos_size} avg={(entry.get('order') or {}).get('avg_price')}")
        check("SL 腿已挂出且带本 bot 前缀",
              any("smk" in str((o.get("order") or {}).get("text", ""))
                  for o in (entry.get("sl_orders") or [])),
              str([(o.get("order") or {}).get("text") for o in (entry.get("sl_orders") or [])]))
        check("TP 腿已挂出", bool(entry.get("tp_orders")))
        check("SL/TP 与入场同时挂（零裸仓窗口）", entry.get("hang_mode") == "simultaneous")
        # 不变量：挂出的 SL 张数 = 本档计划张数（= exit_size），不是差额
        sl_sizes = [abs(float((o.get("order") or {}).get("size") or 0))
                    for o in (entry.get("sl_orders") or [])]
        check("不变量：SL 张数 = 持仓全量", sl_sizes == [pos_size],
              f"sl={sl_sizes} pos={pos_size}")

        tj = root / "data" / "bots" / BOT / "logs" / "trades.jsonl"
        rows = [json.loads(x) for x in tj.read_text(encoding="utf-8").splitlines()
                if x.strip()] if tj.is_file() else []
        check("trades.jsonl 有执行留痕（source=watcher）",
              any(x.get("source") == "watcher" for x in rows), f"{len(rows)} 行")
        gb = root / "data" / "bots" / BOT / "state" / "give_back.json"
        check("give_back_pct 已落盘", gb.is_file() and "30" in gb.read_text(encoding="utf-8"))
        # ── 隔离：paper 账户库必须在 --root 内，且不得污染仓库 data/ ──
        store_in_tmp = root / "data" / "bots" / BOT / "paper" / "account.db"
        check("paper 账户库落在 --root 内", store_in_tmp.is_file())
        check("未污染仓库 data/（--root 隔离生效）",
              not (ROOT / "data" / "bots" / BOT).exists())

        # ── 2. 程序层 sweep ──
        print()
        print("=" * 74)
        print("2. 程序层 5 条 sweep（真实 paper 交易所）")
        print("=" * 74)
        from omnialpha.config import load_all_bots
        from omnialpha.executor import Executor
        from omnialpha.watcher import (ProjectPaths, _auto_protect_sweep, _give_back_sweep,
                                       _orphan_sweep, _peak_trail_sweep, _reconcile_sweep)
        paths = ProjectPaths(root)
        bot = load_all_bots(paths.config_dir)[BOT]
        client = bot.create_client()
        ex = Executor(client, label_prefix=bot.label_prefix, root=paths.root,
                      bot_id=bot.bot_id, account_risk=bot.account_risk,
                      manage_positions=bot.manage_positions,
                      allow_close=bot.allow_close, allow_reduce=bot.allow_reduce)
        # 注意签名差异：reconcile / orphan 只收 (bot, paths, *, client, symbols)；
        # 另外三条多一个 `alerted` 限流表（位置参数）。
        sweeps = (
            ("reconcile", lambda: _reconcile_sweep(bot, paths, client=client, symbols=[SYM])),
            ("orphan", lambda: _orphan_sweep(bot, paths, client=client, symbols=[SYM])),
            ("auto_protect", lambda: _auto_protect_sweep(bot, paths, {}, client=client, symbols=[SYM])),
            ("peak_trail", lambda: _peak_trail_sweep(bot, paths, {}, client=client, symbols=[SYM])),
            ("give_back", lambda: _give_back_sweep(bot, paths, {}, client=client, symbols=[SYM])),
        )
        for name, call in sweeps:
            try:
                n = call()
                print(f"  {name:<13} 返回 {n}（无异常）")
                check(f"{name} sweep 无异常", True)
            except Exception as e:  # noqa: BLE001
                check(f"{name} sweep 无异常", False, repr(e))
        check("全覆盖时 auto_protect 不重复补单", ex.ensure_protection(SYM)["skipped"] == "sl_present")

        # ── 3. 单 bot 闸门矩阵 ──
        print()
        print("=" * 74)
        print("3. 单 bot 闸门矩阵（该 bot 配置：三闸门全开）")
        print("=" * 74)
        for act in ("close", "close_all", "reduce_long", "modify_tp_sl"):
            check(f"{act} 放行", ex._management_gate(act, None) == "")
        locked = Executor(client, label_prefix="smk", root=paths.root, bot_id=BOT,
                          account_risk=bot.account_risk, manage_positions=False)
        for act in ("close", "close_all", "reduce_long", "modify_tp_sl"):
            check(f"manage_positions=false 时 {act} 被拒",
                  locked._management_gate(act, None).startswith("MANAGE_POSITIONS_OFF"))

        # ── 4. 部分覆盖 → 不补差额 + 告警 + 限流 ──
        print()
        print("=" * 74)
        print("4. 部分覆盖（把 SL 换成只覆盖 1 张 / 持仓 3 张）")
        print("=" * 74)
        for o in list(client.list_price_orders(SYM)):
            if str(o.get("text", "")).endswith("-sl") and o.get("status") == "untriggered":
                client.cancel_price_order(o.get("id"))
        client.place_price_order({
            "initial": {"contract": SYM, "size": -1, "price": "0", "tif": "ioc",
                        "reduce_only": True, "text": "t-smk-sl"},
            "trigger": {"rule": 2, "price_type": 0, "price": "81500"}})
        out = ex.ensure_protection(SYM)
        check("部分覆盖 → skipped=partial_coverage", out.get("skipped") == "partial_coverage",
              json.dumps(out, ensure_ascii=False)[:150])
        live_before = len([o for o in client.list_price_orders(SYM)
                           if o.get("status") == "untriggered"])
        alerted: dict = {}
        n1 = _auto_protect_sweep(bot, paths, alerted, client=client, symbols=[SYM])
        live_after = len([o for o in client.list_price_orders(SYM)
                          if o.get("status") == "untriggered"])
        check("程序未补差额单（写操作 0）", n1 == 0 and live_after == live_before,
              f"{live_before} -> {live_after}")
        ap = root / "data" / "bots" / BOT / "state" / "alerts.json"
        al = json.loads(ap.read_text(encoding="utf-8"))["alerts"] if ap.is_file() else []
        check("已告警 unprotected_position", len(al) == 1 and al[0]["type"] == "unprotected_position",
              al[0]["detail"][:80] if al else "无告警")
        _auto_protect_sweep(bot, paths, alerted, client=client, symbols=[SYM])
        al2 = json.loads(ap.read_text(encoding="utf-8"))["alerts"]
        check("告警按 3600s 限流", len(al2) == 1, f"{len(al)} -> {len(al2)}")

        print()
        print("=" * 74)
        print(f"结论：PASS {len(OK)}  FAIL {len(BAD)}")
        for b in BAD:
            print(f"  ✗ {b}")
        print("=" * 74)
        return 1 if BAD else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
