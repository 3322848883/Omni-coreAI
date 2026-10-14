# -*- coding: utf-8 -*-
"""本地多轮 paper 实跑：连续 N 轮信号 + 程序层 sweep，逐轮打状态表。

与 `e2e_local_smoke.py`（单轮 + 断言电池）互补：这个脚本关注**跨轮演化** ——
建仓 → 加仓 → 移损 → 部分覆盖 → 减仓 → 全平，每一轮都跑真实入口
（`run_bot_once` → `process_file` → `execute_signal` → 真 paper 交易所引擎）
并紧跟 5 条程序层 sweep（reconcile / orphan / auto_protect / peak_trail / give_back）。

**不依赖 LLM**：信号直接投 inbox，跳过 plan-loop（本地沙箱出网受限，LLM 走不通）。

逐轮打印：持仓 / 挂单 / 保护单（价·张数）/ 权益 / 告警 / 吞异常计数。
收尾打印：`trades.jsonl` 按 source 汇总 + 不变量检查 + 仓库 `data/` 未被污染。

跑法：
    python scripts/e2e_local_rounds.py            # 默认 6 轮
    python scripts/e2e_local_rounds.py --rounds 3
退出码：0 = 全程无异常且不变量成立；1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SYM = "BTC_USDT"
BOT = "rounds-paper"
OK, BAD = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (OK if cond else BAD).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return cond


BASELINE = """\
bot_id: {bot}
enabled: false          # 基线 enabled 一律被忽略；启用走 bots.local
env: paper
symbols: [{sym}]
label_prefix: rnd
order_scope: own
require_sl: true
manage_positions: true
allow_close: true
allow_reduce: true
paper:
  feed_exchange: gate
  initial_capital: 10000
  leverage: 10
"""

OVERLAY = """\
enabled: true
account_risk:
  auto_protect: true
  auto_protect_sl_pct: 2.0
"""


def _snapshot(client, ex, paths, bot) -> dict:
    """本轮状态快照。"""
    pos = ex._symbol_positions(SYM)
    size = abs(int(pos[0]["size"])) if pos else 0
    orders = client.list_orders(SYM) or []
    prot = client.list_price_orders(SYM) or []
    own_sl = ex._owned_sl_size(SYM, ex._own_prefix(""))
    try:
        acct = client.get_account() or {}
        equity = acct.get("total")
    except Exception:  # noqa: BLE001
        equity = None
    return {
        "pos": size,
        "orders": len(orders),
        "prot": [(str(p.get("text")), p.get("size"), p.get("trigger_price"))
                 for p in prot],
        "sl_cover": own_sl,
        "equity": equity,
    }


def _sweeps(bot, paths, client, alerted) -> dict:
    from omnialpha.watcher import (_auto_protect_sweep, _give_back_sweep,
                                   _orphan_sweep, _peak_trail_sweep, _reconcile_sweep)
    out = {}
    for name, call in (
        ("reconcile", lambda: _reconcile_sweep(bot, paths, client=client, symbols=[SYM])),
        ("orphan", lambda: _orphan_sweep(bot, paths, client=client, symbols=[SYM])),
        ("auto_protect", lambda: _auto_protect_sweep(bot, paths, alerted,
                                                     client=client, symbols=[SYM])),
        ("peak_trail", lambda: _peak_trail_sweep(bot, paths, alerted,
                                                 client=client, symbols=[SYM])),
        ("give_back", lambda: _give_back_sweep(bot, paths, alerted,
                                               client=client, symbols=[SYM])),
    ):
        try:
            out[name] = call()
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERR {e!r}"
            check(f"{name} sweep 无异常", False, repr(e))
    return out


def _alerts(root: Path) -> list:
    p = root / "data" / "bots" / BOT / "state" / "alerts.json"
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("alerts") or []
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR, format="    [%(levelname)s] %(message)s")

    from omnialpha import observability
    from omnialpha.config import load_all_bots
    from omnialpha.executor import Executor
    from omnialpha.watcher import ProjectPaths, run_bot_once

    root = Path(tempfile.mkdtemp(prefix="omnialpha_rounds_"))
    try:
        (root / "config" / "bots").mkdir(parents=True)
        (root / "config" / "bots.local").mkdir(parents=True)
        inbox = root / "data" / "bots" / BOT / "inbox"
        inbox.mkdir(parents=True)
        (root / "config" / "bots" / f"{BOT}.yaml").write_text(
            BASELINE.format(bot=BOT, sym=SYM), encoding="utf-8")
        (root / "config" / "bots.local" / f"{BOT}.yaml").write_text(OVERLAY, encoding="utf-8")

        paths = ProjectPaths(root)
        bot = load_all_bots(paths.config_dir)[BOT]
        client = bot.create_client()
        ex = Executor(client, label_prefix=bot.label_prefix, root=paths.root,
                      bot_id=bot.bot_id, account_risk=bot.account_risk,
                      manage_positions=bot.manage_positions,
                      allow_close=bot.allow_close, allow_reduce=bot.allow_reduce)
        alerted: dict = {}

        def mark() -> float:
            return float(client.get_last_price(SYM))

        def write_signal(i: int, payload: dict) -> None:
            (inbox / f"{i:02d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        print("=" * 100)
        print(f"本地多轮 paper 实跑：{BOT} / {SYM} / 目标 {args.rounds} 轮"
              f"（auto_protect=live 2.0%）")
        print("=" * 100)
        hdr = (f"{'轮':<3}{'动作':<14}{'持仓':>5}{'普通单':>7}{'保护单':>7}"
               f"{'SL覆盖':>7}{'权益':>10}  备注")
        print(hdr)
        print("-" * 100)

        rounds = args.rounds
        rows = []
        for i in range(1, rounds + 1):
            m = mark()
            note = ""
            if i == 1:
                sig = {"action": "open_long", "symbol": SYM, "size_usd": 25,
                       "type": "market", "tp": round(m * 1.02, 1),
                       "sl": round(m * 0.985, 1), "tp_mode": "trigger",
                       "sl_mode": "trigger", "tp_type": "market", "sl_type": "market",
                       "trigger_price_type": "mark", "give_back_pct": 30,
                       "label": "r-open", "meta": {"signal_id": f"r{i}"}}
                act = "open_long"
            elif i == 2:
                sig = {"action": "add_long", "symbol": SYM, "size_usd": 15,
                       "type": "market", "label": "r-add",
                       "meta": {"signal_id": f"r{i}"}}
                act = "add_long"
            elif i == 3:
                sig = {"action": "modify_tp_sl", "symbol": SYM, "side": "long",
                       "sl": round(m * 0.99, 1), "label": "r-trail",
                       "meta": {"signal_id": f"r{i}"}}
                act = "modify_tp_sl"
            elif i == 4:
                # 人为把 SL 拆成**部分覆盖**（模拟「SL 被外部撤掉一半」）→
                # 期望：程序只告警不补差额（B5 的不变量）。
                sig = None
                act = "(拆部分SL)"
                cut = False
                for p in list(client.list_price_orders(SYM)):
                    if str(p.get("text", "")).endswith("-sl") and not cut:
                        client.cancel_price_order(p.get("id"))
                        cut = True
                note = "手动撤掉整条 SL"
            elif i == 5:
                sig = {"action": "reduce_long", "symbol": SYM, "size": 2,
                       "label": "r-reduce", "meta": {"signal_id": f"r{i}"}}
                act = "reduce_long"
            else:
                sig = {"action": "close", "symbol": SYM, "side": "long",
                       "label": "r-close", "meta": {"signal_id": f"r{i}"}}
                act = "close"

            if sig is not None:
                write_signal(i, sig)
                run_bot_once(bot, paths)
            sw = _sweeps(bot, paths, client, alerted)
            snap = _snapshot(client, ex, paths, bot)
            al = _alerts(root)
            rows.append((i, act, snap, sw, len(al)))
            print(f"{i:<3}{act:<14}{snap['pos']:>5}{snap['orders']:>7}"
                  f"{len(snap['prot']):>7}{snap['sl_cover']:>7}"
                  f"{str(snap['equity']):>10}  {note}")
            if snap["prot"]:
                for t, sz, tp in snap["prot"]:
                    print(f"      · {t:<18} size={sz:<5} trigger={tp}")
            # sweep 明细（只打有动作/有跳过的）
            for k, v in sw.items():
                if isinstance(v, dict):
                    key = v.get("skipped") or v.get("cancelled") or v.get("placed")
                    if key:
                        print(f"      ~ {k}: {key}"
                              + (f"  {v.get('error')}" if v.get("error") else ""))
                elif v:
                    print(f"      ~ {k}: {v}")

        # ── 不变量与收尾 ──
        print("-" * 100)
        print("不变量与留痕：")
        final = rows[-1][2]
        check("末轮已全平（持仓 0）", final["pos"] == 0, f"pos={final['pos']}")
        check("末轮无遗留保护单（孤儿已清）", len(final["prot"]) == 0,
              str(final["prot"]))
        over = [f"R{i}: cover={s['sl_cover']} pos={s['pos']}"
                for i, _a, s, _sw, _n in rows if s["sl_cover"] > s["pos"]]
        check("每轮 SL 覆盖 ≤ 持仓（程序不挂超配保护单）", not over, "; ".join(over))

        # 程序对「部分覆盖」的**设计行为**：检测 + 拒绝补差额（而不是悄悄补出部分出场）。
        # 判据取**告警明细**（`_auto_protect_sweep` 返回的是计数，不是 dict）。
        al = _alerts(root)
        partial_hits = [a for a in al if "partial_coverage" in str(a.get("detail", ""))]
        check("程序检测到部分覆盖并拒绝补差额（partial_coverage）",
              bool(partial_hits),
              partial_hits[0].get("detail", "")[:80] if partial_hits else "未发现")

        types = sorted({a.get("type") for a in al})
        print(f"  告警 {len(al)} 条，类型 {types}")
        check("存在未受保护/孤儿告警（程序不静默）",
              any(("unprotected" in str(t)) or ("orphan" in str(t)) for t in types),
              f"types={types}")

        tj = root / "data" / "bots" / BOT / "logs" / "trades.jsonl"
        srcs: dict = {}
        for line in (tj.read_text(encoding="utf-8").splitlines() if tj.is_file() else []):
            if line.strip():
                s = json.loads(line).get("source") or "?"
                srcs[s] = srcs.get(s, 0) + 1
        print(f"  trades.jsonl 按 source 汇总：{srcs}")
        check("有 watcher 执行留痕", srcs.get("watcher", 0) > 0, str(srcs))

        cnt = observability.counts()
        print(f"  observability 计数：{dict(list(cnt.items())[:6])}")

        check("paper 账户库落在临时根内",
              (root / "data" / "bots" / BOT / "paper" / "account.db").is_file())
        check("未污染仓库 data/", not (ROOT / "data" / "bots" / BOT).exists())

        print()
        print("=" * 100)
        print(f"结论：PASS {len(OK)}  FAIL {len(BAD)}")
        if BAD:
            for b in BAD:
                print(f"  ✗ {b}")
        print("=" * 100)
        return 1 if BAD else 0
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
