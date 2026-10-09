# -*- coding: utf-8 -*-
"""T13：画像 / 盈亏 / 衰减**按币**（多币各自独立统计，单币逐字不变）。

覆盖 spec `symbol-as-parameter.md` §S2.4⑩ / S2.3 #8：

- 画像的权威口径是**账本投影**（`fills.realised_pnl`），而它原先只给**合并值** ——
  多币下「这个币到底赚没赚」答不出来（一币亏一币赚会互相抵消）。
- 提示摘要多币时按币各列一条；只有**一个**币有成交时输出与改动前逐字相同（I11）。
- 画像**移出稳定前缀**：它每平一笔就变，放在 system 里会让整段缓存前缀失效
  （system 是最长的一段：人设+契约+工具）→ 移到动态区（user）。
- `exchange_pnl`（live bot 的画像来源）同样给按币层，形状与 paper 侧一致。
- 没有 `contract` 列的库要能读（不能因此整段失败、静默退回文件累加值）。

记忆本身是 **bot 独立**的（`data/bots/<id>/state/`）—— 这里测的是同一个 bot
**内部多币**的隔离，不是跨 bot 共享。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.memory.context import build_context  # noqa: E402
from omnialpha.memory.profile import MemoryProfile  # noqa: E402
from omnialpha.paper.store import realized_pnl_stats  # noqa: E402

BOT = "b1"


def _db_path(root: Path) -> Path:
    return root / "data" / "bots" / BOT / "paper" / "account.db"


def _ledger(root: Path, rows: list, *, with_contract: bool = True) -> None:
    """造一个最小 paper 账本。`rows = [(contract, realised_pnl)]`。"""
    db = _db_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    cols = "fill_time INTEGER, side TEXT, price REAL, size REAL, fee REAL," \
           " realised_pnl REAL, role TEXT, order_id TEXT, kind TEXT"
    if with_contract:
        cols = "fill_time INTEGER, contract TEXT, side TEXT, price REAL, size REAL," \
               " fee REAL, realised_pnl REAL, role TEXT, order_id TEXT, kind TEXT"
    con = sqlite3.connect(str(db))
    con.execute(f"CREATE TABLE fills (id INTEGER PRIMARY KEY, {cols})")
    for i, (c, p) in enumerate(rows):
        base = (i, c, "sell", 1.0, 1.0, 0.0, p, "taker", f"o{i}", "trade") if with_contract \
            else (i, "sell", 1.0, 1.0, 0.0, p, "taker", f"o{i}", "trade")
        ph = ",".join("?" * len(base))
        con.execute(f"INSERT INTO fills VALUES (NULL,{ph})", base)
    con.commit()
    con.close()


class TestLedgerStatsPerSymbol(unittest.TestCase):
    def test_two_symbols_have_independent_stats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [("BTC_USDT", 10.0), ("BTC_USDT", -4.0),
                           ("ETH_USDT", 1.0), ("ETH_USDT", -5.0), ("ETH_USDT", -2.0)])
            st = MemoryProfile(root, BOT).ledger_stats()
            self.assertEqual(st["total_trades"], 5, "合并值仍是 5 笔")
            by = st["by_symbol"]
            self.assertEqual(by["BTC_USDT"]["total_trades"], 2)
            self.assertEqual(by["BTC_USDT"]["win_count"], 1)
            self.assertAlmostEqual(by["BTC_USDT"]["total_pnl_usd"], 6.0)
            self.assertEqual(by["ETH_USDT"]["total_trades"], 3)
            self.assertEqual(by["ETH_USDT"]["win_count"], 1)
            self.assertAlmostEqual(by["ETH_USDT"]["total_pnl_usd"], -6.0)
            # 派生字段也按币算（胜率/均盈亏）
            self.assertAlmostEqual(by["BTC_USDT"]["win_rate"], 0.5)
            self.assertAlmostEqual(by["ETH_USDT"]["win_rate"], round(1 / 3, 3))

    def test_ledger_without_contract_column_still_reads(self):
        """没有 `contract` 列的库不能整段读失败（那会让画像静默退回文件值）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [("", 10.0), ("", -4.0)], with_contract=False)
            st = MemoryProfile(root, BOT).ledger_stats()
            self.assertEqual(st["total_trades"], 2)
            self.assertNotIn("by_symbol", st, "没有币维度就不该编一个")


class TestPromptSummaryPerSymbol(unittest.TestCase):
    def test_single_symbol_unchanged(self):
        """单币（只有一个币有成交）→ 输出与改动前**逐字相同**（I11）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [("BTC_USDT", 10.0), ("BTC_USDT", -4.0)])
            s = MemoryProfile(root, BOT).prompt_summary()
            self.assertEqual(s, "历史表现: 2笔交易, 胜率50%, 均盈亏3.0u")

    def test_multi_symbol_lists_each(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [("BTC_USDT", 10.0), ("ETH_USDT", -4.0)])
            s = MemoryProfile(root, BOT).prompt_summary()
            self.assertTrue(s.startswith("历史表现(按币):"), s)
            self.assertIn("BTC_USDT: 1笔交易, 胜率100%, 均盈亏10.0u", s)
            self.assertIn("ETH_USDT: 1笔交易, 胜率0%, 均盈亏-4.0u", s)

    def test_no_trades_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(MemoryProfile(Path(td), BOT).prompt_summary(), "")


class TestProfileOutOfStablePrefix(unittest.TestCase):
    """画像每轮可能变 → 不能进稳定前缀（否则整段 system 缓存失效）。"""

    def test_profile_lands_in_dynamic_part(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [("BTC_USDT", 10.0), ("ETH_USDT", -4.0)])
            ctx = build_context(root, BOT, system_prompt="SYS")
            self.assertEqual(ctx["system"], "SYS", "system 必须只有人设，画像不得进来")
            self.assertNotIn("[画像]", ctx["system"])
            self.assertIn("[画像]", ctx["user"])
            self.assertIn("历史表现(按币)", ctx["user"])

    def test_no_profile_block_when_no_trades(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = build_context(Path(td), BOT, system_prompt="SYS")
            self.assertNotIn("[画像]", ctx["user"], "没成交就不该有这一段噪音")


class TestRealizedStatsPerContract(unittest.TestCase):
    def test_optional_layer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [("BTC_USDT", 10.0), ("ETH_USDT", -4.0)])
            plain = realized_pnl_stats(_db_path(root))
            self.assertEqual(set(plain), {"trades", "wins", "pnl", "worst"},
                             "不带 by_contract 时形状与改动前相同")
            with_by = realized_pnl_stats(_db_path(root), by_contract=True)
            self.assertEqual(sorted(with_by["by_contract"]), ["BTC_USDT", "ETH_USDT"])
            self.assertEqual(with_by["by_contract"]["ETH_USDT"]["pnl"], -4.0)

    def test_contract_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _ledger(root, [("BTC_USDT", 10.0), ("ETH_USDT", -4.0)])
            one = realized_pnl_stats(_db_path(root), contract="ETH_USDT")
            self.assertEqual(one["trades"], 1)
            self.assertEqual(one["pnl"], -4.0)
            self.assertEqual(one["contract"], "ETH_USDT")


if __name__ == "__main__":
    unittest.main()
