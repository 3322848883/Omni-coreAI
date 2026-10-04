# -*- coding: utf-8 -*-
"""画像的**账本投影**：核心统计以 paper 账本为准。

背景（实测）：文件式累加（`record_trade`）只记「被检测到的平仓事件」，而
- 单 bot 路径（`run_once`）根本没有平仓钩子 → 整个 `*-paper` 舰队的画像一直是空的
- SL/TP 触发是交易所侧成交、没有信号 → 永远检测不到

结果：**459 笔真实平仓只记了 1 笔**，且漏掉的恰好包含止损那些负面样本
→ 画像向 AI 报的胜率被系统性抬高。

改为读时投影（幂等、不依赖触发点）后，覆盖全部平仓、全部 bot。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from omnialpha.memory import MemoryProfile

BOT = "proj-bot"


def _ledger(root: Path, pnls: list[float], bot: str = BOT) -> None:
    """造一个最小的 paper 账本（只需要 fills.realised_pnl）。"""
    db = root / "data" / "bots" / bot / "paper" / "account.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("create table fills (id integer primary key, realised_pnl real)")
    for i, v in enumerate(pnls):
        con.execute("insert into fills (id, realised_pnl) values (?, ?)", (i + 1, v))
    con.commit()
    con.close()


def _profile_file(root: Path, rec: dict, bot: str = BOT) -> None:
    p = root / "data" / "bots" / bot / "state" / "memory_profile.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


class TestProfileLedgerProjection(unittest.TestCase):
    def test_ledger_stats_counts_all_closing_fills(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 3 笔平仓：2 盈 1 亏（含开仓的 0 不计）
            _ledger(root, [1.5, -0.5, 2.0, 0.0])
            st = MemoryProfile(root, BOT).ledger_stats()
            self.assertEqual(st["total_trades"], 3, "开仓成交（0）不该算成一笔交易")
            self.assertEqual(st["win_count"], 2)
            self.assertAlmostEqual(st["total_pnl_usd"], 3.0)
            self.assertAlmostEqual(st["max_drawdown_usd"], -0.5)

    def test_load_overlays_ledger_over_file(self):
        """文件说 1 笔，账本说 3 笔 → 以账本为准。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _profile_file(root, {"total_trades": 1, "win_count": 1,
                                 "total_pnl_usd": 0.01, "avg_hold_rounds": 1.0,
                                 "max_drawdown_usd": 0.0, "updated_at": 1})
            _ledger(root, [10.0, -4.0, 2.0])
            d = MemoryProfile(root, BOT).load()
            self.assertEqual(d["total_trades"], 3, f"画像没以账本为准：{d}")
            self.assertEqual(d["win_count"], 2)
            self.assertAlmostEqual(d["total_pnl_usd"], 8.0)
            self.assertAlmostEqual(d["win_rate"], round(2 / 3, 3))
            self.assertAlmostEqual(d["max_drawdown_usd"], -4.0)
            # 账本给不出的字段仍来自文件
            self.assertEqual(d["avg_hold_rounds"], 1.0)

    def test_prompt_summary_uses_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [10.0, -10.0])
            s = MemoryProfile(root, BOT).prompt_summary()
            self.assertIn("2笔交易", s)
            self.assertIn("胜率50%", s)

    def test_no_ledger_falls_back_to_file(self):
        """live 账户没有 paper 账本 → 沿用文件里的累加值。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _profile_file(root, {"total_trades": 7, "win_count": 4,
                                 "total_pnl_usd": 12.5, "avg_hold_rounds": 3.0,
                                 "max_drawdown_usd": -2.0, "updated_at": 1})
            d = MemoryProfile(root, BOT).load()
            self.assertEqual(d["total_trades"], 7)
            self.assertEqual(d["win_count"], 4)
            self.assertIsNone(MemoryProfile(root, BOT).ledger_stats())

    def test_ledger_without_closes_falls_back_to_file(self):
        """账本存在但还没有平仓 → 不能把画像清零。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _profile_file(root, {"total_trades": 5, "win_count": 3,
                                 "total_pnl_usd": 9.0, "avg_hold_rounds": 2.0,
                                 "max_drawdown_usd": -1.0, "updated_at": 1})
            _ledger(root, [0.0, 0.0])          # 只有开仓成交
            self.assertIsNone(MemoryProfile(root, BOT).ledger_stats())
            self.assertEqual(MemoryProfile(root, BOT).load()["total_trades"], 5)

    def test_projection_is_idempotent(self):
        """投影是「读时算」的 —— 读多少次都一样，不累加。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [3.0, -1.0])
            p = MemoryProfile(root, BOT)
            first = p.load()
            for _ in range(5):
                self.assertEqual(p.load(), first)


if __name__ == "__main__":
    unittest.main()
