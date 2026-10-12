# -*- coding: utf-8 -*-
"""修复前后 LLM 上下文对比（brooks-eth 48h 实盘审计数据重构）。

数据口径：审计结论 11 笔 / 4 胜 7 亏 / 合计 -4.85U（平均盈 0.26 × 4 = +1.04，
平均亏 0.84 × 7 = -5.89，1.04 - 5.89 = -4.85 吻合）。时间戳按审计窗口
（10-10 ~ 10-12 UTC）均匀铺开，末尾 5 笔连亏 —— 正是实盘「连亏后继续开仓」的状态。

跑法：python scripts/_demo_pnl_context.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.memory.context import _exec_tag, _pnl_block, build_context

BOT = "brooks-eth"
# 48h 审计序列（旧→新），按统计重构；末尾 4 笔连亏模拟实盘连亏状态
PNLS = [+0.26, -0.84, +0.26, -0.84, -0.84, +0.26, -0.84, +0.26, -0.84, -0.84, -0.84]
EQUITY = 150.0   # 审计期账户权益量级


def build_root(tmp: Path, *, with_pnl: bool) -> Path:
    root = tmp / ("with" if with_pnl else "without")
    (root / "data" / "bots" / BOT / "state").mkdir(parents=True, exist_ok=True)
    if with_pnl:
        now = int(time.time())
        fills = [{"key": str((now - 3600 * (11 - i)) * 1_000_000),
                  "pnl": p, "contract": "ETH_USDT", "side": "",
                  "time": now - 3600 * (11 - i), "source": "trigger"}
                 for i, p in enumerate(PNLS)]
        rec = {"cursor": "0", "active_cursor": "0",
               "totals": {"trades": 11, "wins": 4, "pnl": -4.85, "worst": -0.84,
                          "by_contract": {"ETH_USDT": {"trades": 11, "wins": 4,
                                                       "pnl": -4.85, "worst": -0.84}}},
               "fills": fills, "excluded": [], "updated_at": now}
        (root / "data" / "bots" / BOT / "state" / "exchange_pnl.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return root


def main() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ── 修复前：journal 里没有 pnl，[近况] 只能看到决策与推理 ──
        root = build_root(tmp, with_pnl=False)
        ctx = build_context(root, BOT, system_prompt="(system)", snapshot_text="(快照)")
        before_recent = ("2026-10-12T09:15Z: open_long [orders=2] "
                         "— 15m 三推上升结构，回踩 EMA20 接多……")
        before = "\n".join([
            "[近况]",
            before_recent,
            "2026-10-12T09:00Z: open_long — 上轮止损后结构未破坏，同位再接……",
            "2026-10-12T08:45Z: open_long — 连续阴跌后首次站回 EMA20……",
        ])
        print("=" * 72)
        print("【修复前】LLM 每轮看到的（无盈亏数据 —— 金鱼记忆）")
        print("=" * 72)
        print(before)
        print("\n→ 模型视角：三连开、无盈亏回看，连亏条款（HC-09）无从触发。\n")

        # ── 修复后：[盈亏] 段 + [近况] 行 realized 标记 ──
        root = build_root(tmp, with_pnl=True)
        ctx = build_context(root, BOT, system_prompt="(system)", snapshot_text="(快照)",
                            account_equity=EQUITY)
        rows = [
            {"cycle_id": "2026-10-12T09:15Z", "decision": "open_long",
             "reasoning": "15m 三推上升结构，回踩 EMA20 接多……",
             "exec_result": {"orders": 2, "trigger": "kline_close",
                             "closed": 2, "realized_pnl": -1.68}},
            {"cycle_id": "2026-10-12T09:00Z", "decision": "open_long",
             "reasoning": "上轮止损后结构未破坏，同位再接……",
             "exec_result": {"orders": 0, "trigger": "kline_close",
                             "closed": 1, "realized_pnl": -0.84}},
            {"cycle_id": "2026-10-12T08:45Z", "decision": "hold",
             "reasoning": "信号不清晰，观望……",
             "exec_result": {"orders": 0, "trigger": "kline_close"}},
        ]
        after = "\n".join([
            f"{r['cycle_id']}: {r['decision']}{_exec_tag(r)} — {r['reasoning']}"
            for r in rows
        ])
        print("=" * 72)
        print("【修复后】LLM 每轮看到的（同一账户状态）")
        print("=" * 72)
        print(_pnl_block(root, BOT, EQUITY))
        print()
        print("[近况]")
        print(after)
        print()
        print("=" * 72)
        print("差异点")
        print("=" * 72)
        for line in [
            "1. [盈亏·已实现] 段：最近平仓序列 + 连亏计数 + 当日已实现（占权益 %）",
            "   → HC-09（连亏≥3 减半）/ HC-08（当日亏损闸门）首次有了数据依据",
            "2. [近况] 行 [realized=±x]：上一轮决策之后的平仓结果直接可见",
            "   → 「上一笔亏了多少钱、连亏几笔」不再需要 LLM 自己推断",
            "3. journal exec_result 携带 closed/realized_pnl → journal_lookup 回看也有 pnl",
            "4. 无数据/文件缺失时整段不渲染，绝不喂假数字",
        ]:
            print(line)
        out = Path(__file__).resolve().parents[1] / "reports" / "pnl_context_before_after.txt"
        out.parent.mkdir(exist_ok=True)
        out.write_text("修复后渲染样例：\n\n" + _pnl_block(root, BOT, EQUITY) +
                       "\n\n[近况]\n" + after + "\n", encoding="utf-8")
        print(f"\n样例已存: {out}")


if __name__ == "__main__":
    main()
