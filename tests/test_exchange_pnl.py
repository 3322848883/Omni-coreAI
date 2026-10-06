# -*- coding: utf-8 -*-
"""交易所成交明细 → 已实现盈亏投影。

背景：live bot 的画像一直是空的 —— `MemoryProfile.ledger_stats()` 只认 paper 账本，
而 live bot 没有；回退路径 `record_trade()` 的唯一调用者在 persona 路径上。

数据源**不能**是本地日志：SL/TP 触发是交易所侧成交、没有本地信号。实测某 bot 的
339 行 `trades.jsonl` 里只有 3 条带 `realized_pnl`、83 条 `receipts/` 回执里只有
4 条 —— 用它投影会漏掉全部止损，而止损正是负面样本，胜率会被系统性抬高
（`profile.py` 记过一次同类事故：459 笔真实平仓只记了 1 笔）。

所以从 `my_trades` 按游标幂等摄取。本文件钉住幂等、增量、失败不推进游标三条性质。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.memory.exchange_pnl import KEEP_FILLS, load, stats, sync

BOT = "pnl-bot"


class _TradeClient:
    def __init__(self, rows=None, fail=False):
        self.rows = list(rows or [])
        self.fail = fail
        self.calls: list[dict] = []

    def list_my_trades(self, contract=None, limit=1000):
        self.calls.append({"contract": contract, "limit": limit})
        if self.fail:
            raise RuntimeError("boom")
        return self.rows


class TestSync(unittest.TestCase):
    def test_first_sync_counts_only_closed_fills(self):
        rows = [
            {"id": "1", "pnl": "0", "contract": "BTC_USDT"},      # 开仓
            {"id": "2", "pnl": "-1.5", "contract": "BTC_USDT"},   # 止损
            {"id": "3", "pnl": "2.5", "contract": "BTC_USDT"},    # 止盈
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = sync(root, BOT, _TradeClient(rows))
            self.assertTrue(out["ok"])
            self.assertEqual(out["added"], 2, "pnl=0 的开仓成交不该计入")
            st = stats(root, BOT)
            self.assertEqual(st["trades"], 2)
            self.assertEqual(st["wins"], 1)
            self.assertAlmostEqual(st["pnl"], 1.0)
            self.assertAlmostEqual(st["worst"], -1.5)

    def test_idempotent_on_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = _TradeClient([{"id": "2", "pnl": "1.0"}])
            sync(root, BOT, c)
            out2 = sync(root, BOT, c)          # 再拉同一批
            self.assertEqual(out2["added"], 0, "重复同步不该重复计入")
            self.assertEqual(stats(root, BOT)["trades"], 1)

    def test_incremental_after_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sync(root, BOT, _TradeClient([{"id": "5", "pnl": "1.0"}]))
            out = sync(root, BOT, _TradeClient([
                {"id": "5", "pnl": "1.0"}, {"id": "7", "pnl": "-0.5"}]))
            self.assertEqual(out["added"], 1)
            st = stats(root, BOT)
            self.assertEqual(st["trades"], 2)
            self.assertAlmostEqual(st["worst"], -0.5)

    def test_failure_does_not_advance_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = sync(root, BOT, _TradeClient(fail=True))
            self.assertFalse(out["ok"])
            self.assertIn("error", out)
            self.assertEqual(load(root, BOT)["cursor"], "",
                             "拉取失败时推进游标会让那一段被永久跳过")
            self.assertIsNone(stats(root, BOT))

    def test_cursor_advances_even_without_new_closes(self):
        """只有开仓成交时也要推游标，否则每次重复扫同一段。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = sync(root, BOT, _TradeClient([{"id": "42", "pnl": "0"}]))
            self.assertEqual(out["added"], 0)
            self.assertEqual(load(root, BOT)["cursor"], "42")

    def test_missing_id_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = sync(root, BOT, _TradeClient([{"pnl": "1.0"}, {"id": "9", "pnl": "2.0"}]))
            self.assertEqual(out["added"], 1)

    def test_worst_remembers_minimum_across_syncs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sync(root, BOT, _TradeClient([{"id": "1", "pnl": "-3.0"}]))
            sync(root, BOT, _TradeClient([{"id": "1", "pnl": "-3.0"},
                                          {"id": "2", "pnl": "5.0"}]))
            self.assertAlmostEqual(stats(root, BOT)["worst"], -3.0,
                                   msg="worst 应记住最差那笔，不被后续盈利覆盖")

    def test_all_wins_worst_is_smallest_win(self):
        """全盈利时 worst 是最小的**盈利**，不是 0。

        拿 0 顶替会读成「有一笔不赚不亏」，是假信息（`paper/store.py` 的
        `min(vals)` 口径一致）。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sync(root, BOT, _TradeClient([{"id": "1", "pnl": "2.0"},
                                          {"id": "2", "pnl": "3.0"}]))
            self.assertAlmostEqual(stats(root, BOT)["worst"], 2.0)

    def test_fills_capped_but_totals_unaffected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [{"id": str(i), "pnl": "1.0"} for i in range(1, KEEP_FILLS + 60)]
            sync(root, BOT, _TradeClient(rows))
            rec = load(root, BOT)
            self.assertEqual(len(rec["fills"]), KEEP_FILLS, "明细应被截断")
            self.assertEqual(stats(root, BOT)["trades"], KEEP_FILLS + 59,
                             "统计走 totals，不该被明细上限影响")


class TestStats(unittest.TestCase):
    def test_empty_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(stats(Path(td), BOT), "没有任何成交时应返回 None")

    def test_corrupt_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / "data" / "bots" / BOT / "state" / "exchange_pnl.json"
            p.parent.mkdir(parents=True)
            p.write_text("{not json", encoding="utf-8")
            self.assertIsNone(stats(root, BOT))

    def test_shape_matches_paper_ledger(self):
        """形状必须与 `realized_pnl_stats` 一致 —— ledger_stats 把两者当同一数据源。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sync(root, BOT, _TradeClient([{"id": "1", "pnl": "-1.0"}]))
            self.assertEqual(set(stats(root, BOT)), {"trades", "wins", "pnl", "worst"})


class TestListMyTradesQuery(unittest.TestCase):
    def _client(self, seen: dict):
        from omnialpha.gate_client import GateClient

        c = GateClient.__new__(GateClient)

        def fake(method, path, qs, body=None):
            seen.update({"method": method, "path": path, "qs": qs})
            return []

        c.rest_signed_request = fake
        return c

    def test_query_params(self):
        seen: dict = {}
        self._client(seen).list_my_trades(contract="BTC_USDT", limit=50, last_id="99")
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], "/api/v4/futures/usdt/my_trades")
        self.assertIn("limit=50", seen["qs"])
        self.assertIn("contract=BTC_USDT", seen["qs"])
        self.assertIn("last_id=99", seen["qs"])

    def test_limit_clamped_to_exchange_max(self):
        seen: dict = {}
        self._client(seen).list_my_trades(limit=99999)
        self.assertIn("limit=1000", seen["qs"])

    def test_optional_params_omitted(self):
        seen: dict = {}
        self._client(seen).list_my_trades()
        self.assertNotIn("contract", seen["qs"])
        self.assertNotIn("last_id", seen["qs"])


class TestLedgerStatsFallsBackToExchange(unittest.TestCase):
    """`MemoryProfile.ledger_stats` 的数据源链：paper 账本 → 交易所投影 → None。

    live bot 没有 paper 账本，而它的画像一直是空的 —— 回退路径 `record_trade()` 的
    唯一调用者在 persona 路径上，单 bot 路径没有平仓钩子。这条链接上之后，
    `prompt_summary()` 才会往 system prompt 里写「历史表现」。
    """

    def test_exchange_projection_used_when_no_paper_ledger(self):
        from omnialpha.memory import MemoryProfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sync(root, BOT, _TradeClient([
                {"id": "1", "pnl": "-1.0"}, {"id": "2", "pnl": "3.0"}]))
            st = MemoryProfile(root, BOT).ledger_stats()
            self.assertIsNotNone(st, "live bot 的画像仍为空 —— 交易所投影没接上")
            self.assertEqual(st["total_trades"], 2)
            self.assertEqual(st["win_count"], 1)
            self.assertAlmostEqual(st["total_pnl_usd"], 2.0)
            self.assertAlmostEqual(st["max_drawdown_usd"], -1.0)

    def test_none_when_nothing_anywhere(self):
        from omnialpha.memory import MemoryProfile

        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(MemoryProfile(Path(td), BOT).ledger_stats())

    def test_prompt_summary_becomes_non_empty(self):
        """最终目的：system prompt 里真的出现「历史表现」。"""
        from omnialpha.memory import MemoryProfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sync(root, BOT, _TradeClient([{"id": "1", "pnl": "2.0"},
                                          {"id": "2", "pnl": "-1.0"}]))
            s = MemoryProfile(root, BOT).prompt_summary()
            self.assertTrue(s, "画像仍为空，prompt_summary 拿不到数据")
            self.assertIn("2笔交易", s)
            self.assertIn("胜率50%", s)


class TestExchangePnlSweep(unittest.TestCase):
    """`run` 的 300s sweep 要把它拉起来（且只对 live）。"""

    @staticmethod
    def _paths(root: Path):
        from types import SimpleNamespace
        return SimpleNamespace(root=root)

    def _bot(self, env="live", client=None, boom=False):
        class _Bot:
            bot_id = BOT
            symbols = ["BTC_USDT"]

            def create_client(self):
                if boom:
                    raise RuntimeError("no creds")
                return client

        _Bot.env = env
        return _Bot()

    def test_skips_non_live_env(self):
        from omnialpha.watcher import _exchange_pnl_sweep

        with tempfile.TemporaryDirectory() as td:
            n = _exchange_pnl_sweep(
                self._bot(env="paper", boom=True), self._paths(Path(td)))
            self.assertEqual(n, 0, "paper 环境不该建 client（平仓本来就在本地账本里）")

    def test_syncs_for_live_env(self):
        from omnialpha.watcher import _exchange_pnl_sweep

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            n = _exchange_pnl_sweep(
                self._bot(client=_TradeClient([{"id": "1", "pnl": "-1.0"}])),
                self._paths(root))
            self.assertEqual(n, 1)
            self.assertEqual(stats(root, BOT)["trades"], 1)

    def test_client_failure_is_swallowed(self):
        from omnialpha.watcher import _exchange_pnl_sweep

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                _exchange_pnl_sweep(self._bot(boom=True), self._paths(Path(td))), 0)

    def test_api_failure_is_swallowed(self):
        from omnialpha.watcher import _exchange_pnl_sweep

        with tempfile.TemporaryDirectory() as td:
            n = _exchange_pnl_sweep(
                self._bot(client=_TradeClient(fail=True)), self._paths(Path(td)))
            self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
