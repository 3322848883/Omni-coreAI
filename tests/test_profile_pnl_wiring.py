# -*- coding: utf-8 -*-
"""画像的 PnL 接线（**调用点层面** —— 原先缺的正是这一层）。

背景：`_update_profile_on_close` 曾写死 `record_trade(pnl_usd=0.0)`，于是一笔
**+50u 的盈利单**被记成「胜率 0%、均盈亏 0u」，而 `prompt_summary()` 会把它拼进
system prompt —— 等于告诉 AI「你的策略一直在输」。

**为什么原来的 48 项记忆测试没抓到**：它们全部**直接调 `record_trade(pnl_usd=50.0)`**，
验证的是方法本身；而生产调用点传的是 0.0，且 `_update_profile_on_close` 在 tests 里
**引用 0 次**。所以这里专测调用点。
"""
from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from omnialpha.memory import MemoryProfile
from omnialpha.persona.orders import SharedOrderStore
from omnialpha.persona.runner import PersonaRunner

OID = "o-pnl0001"
TARGET = "pt-b1"
MEMBER = "pt-b1"


def _runner(root: Path) -> PersonaRunner:
    r = PersonaRunner.__new__(PersonaRunner)      # 不跑 __init__（不需要 LLM/配置）
    r.root = root
    r.group = SimpleNamespace(members=[MEMBER], name="g")
    r.orders = SharedOrderStore(root)
    r.discussion_log = []
    r.log_path = root / "data" / "shared" / "persona_log.jsonl"
    r.log_path.parent.mkdir(parents=True, exist_ok=True)
    return r


def _closed_order(root: Path, *, status: str = "closed") -> None:
    rec = {
        "order_id": OID, "symbol": "BTC_USDT", "group": "g", "side": "long",
        "status": status, "members": [MEMBER], "target_account": TARGET,
        "lifecycle": [
            {"t": 1, "act": "open", "detail": "open_long long", "by": "fusion:weighted_vote"},
            {"t": 2, "act": "close", "detail": "close long", "by": "fusion:weighted_vote"},
        ],
    }
    p = root / "data" / "shared" / "orders" / f"{OID}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def _trade_log(root: Path, *, order_id=OID, realized_pnl=50.0) -> None:
    """写一条目标账户的成交日志（模拟执行器平仓后落的那条）。"""
    p = root / "data" / "bots" / TARGET / "logs" / "trades.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "type": "execution", "bot_id": TARGET, "source": "watcher",
        "order_id": order_id, "plan_cycle": None, "ok": True,
        "steps": [{"action": "close", "symbol": "BTC_USDT", "ok": True,
                   "detail": {"realized_pnl": realized_pnl, "entry_price": 84000}}],
    }, ensure_ascii=False) + "\n", encoding="utf-8")


def _profile(root: Path) -> dict:
    return MemoryProfile(root, MEMBER).load()


class TestProfilePnlWiring(unittest.TestCase):
    def test_profitable_close_records_real_pnl(self):
        """**核心**：+50u 的平仓必须记成 +50u / 胜率 100%。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _closed_order(root)
            _trade_log(root, realized_pnl=50.0)
            _runner(root)._update_profile_on_close(OID)
            p = _profile(root)
            self.assertEqual(p["total_trades"], 1)
            self.assertEqual(p["win_count"], 1, f"盈利单应记胜 1 次，实际 {p}")
            self.assertAlmostEqual(p["total_pnl_usd"], 50.0)
            self.assertIn("胜率100%", MemoryProfile(root, MEMBER).prompt_summary())

    def test_losing_close_records_negative_pnl(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _closed_order(root)
            _trade_log(root, realized_pnl=-20.0)
            _runner(root)._update_profile_on_close(OID)
            p = _profile(root)
            self.assertEqual(p["win_count"], 0)
            self.assertAlmostEqual(p["total_pnl_usd"], -20.0)
            self.assertAlmostEqual(p["max_drawdown_usd"], -20.0)

    def test_no_execution_yet_skips_instead_of_recording_zero(self):
        """**关键回归**：信号还没被执行（没有成交日志）→ 跳过，绝不写 0。

        写 0 会把「尚未成交」变成「一笔 0 盈亏的交易」，进而污染胜率。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _closed_order(root)
            # 故意不写成交日志
            _runner(root)._update_profile_on_close(OID)
            p = _profile(root)
            self.assertEqual(p["total_trades"], 0, f"不该记任何一笔，实际 {p}")
            self.assertEqual(MemoryProfile(root, MEMBER).prompt_summary(), "",
                             "没有真实数据时不应向 AI 报任何画像")

    def test_order_id_mismatch_is_ignored(self):
        """成交日志里是别的单 → 不算数（防串单）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _closed_order(root)
            _trade_log(root, order_id="o-other", realized_pnl=999.0)
            _runner(root)._update_profile_on_close(OID)
            self.assertEqual(_profile(root)["total_trades"], 0)

    def test_multiple_closes_take_last_pnl(self):
        """同一单多次平仓 → 取最后一次的盈亏。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _closed_order(root)
            _trade_log(root, realized_pnl=10.0)
            p = root / "data" / "bots" / TARGET / "logs" / "trades.jsonl"
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "order_id": OID, "steps": [{"detail": {"realized_pnl": -5.0}}],
                }, ensure_ascii=False) + "\n")
            _runner(root)._update_profile_on_close(OID)
            self.assertAlmostEqual(_profile(root)["total_pnl_usd"], -5.0)

    def test_source_no_longer_hardcodes_zero(self):
        """回归钉：不能再写死 `record_trade(pnl_usd=0.0)`。

        **注意别写成「源码不含 `pnl_usd=0.0`」** —— 文档字符串里会提到它
        （解释为什么不再那么写），那样会误报。（这个坑我踩过一次：
        与 `_has_pending_entry` 那条检查同型 —— 匹配到注释里的字面量。）
        所以查的是**真实调用**，并正向断言传的是变量。
        """
        src = inspect.getsource(PersonaRunner._update_profile_on_close)
        self.assertNotIn("record_trade(pnl_usd=0.0", src, "又把盈亏写死成 0 了")
        self.assertIn("record_trade(pnl_usd=pnl", src, "应传入从成交日志回连到的真实盈亏")

    def test_tradelog_records_order_id(self):
        """成交日志必须带 order_id —— 否则上面这条回连根本无从谈起。"""
        from omnialpha.tradelog import TradeLogger
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.jsonl"
            TradeLogger(p).log_execution(
                "pt-b1", {"order_id": OID, "group": "g"}, {"ok": True, "steps": []})
            row = json.loads(p.read_text(encoding="utf-8").strip())
            self.assertEqual(row.get("order_id"), OID,
                             "log_execution 必须把 signal.meta.order_id 落进成交日志")


if __name__ == "__main__":
    unittest.main()
