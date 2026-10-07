# -*- coding: utf-8 -*-
"""交易所平仓历史 → 已实现盈亏投影。

背景：live bot 的画像一直是空的 —— `MemoryProfile.ledger_stats()` 只认 paper 账本，
而 live bot 没有；回退路径 `record_trade()` 的唯一调用者在 persona 路径上。

数据源**不能**是本地日志：SL/TP 触发是交易所侧成交、没有本地信号。实测某 bot 的
339 行 `trades.jsonl` 里只有 3 条带 `realized_pnl`、83 条 `receipts/` 回执里只有
4 条 —— 用它投影会漏掉全部止损，而止损正是负面样本，胜率会被系统性抬高。

数据源也**不能**是 `my_trades`：真机实测它的返回字段里没有 `pnl`
（`['amend_text','biz_info','close_size','contract','create_time','fee','id',
'order_id','point_fee','price','role','size','text']`），只有 size/price/fee，
推不出已实现盈亏。`position_close` 才是权威口径，且它**没有单调 id**，
游标只能用 `time_us`。本文件钉住这些性质。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from omnialpha.memory.exchange_pnl import (KEEP_FILLS, _close_key, is_ours,
                                           load, local_order_ids, stats, sync)

BOT = "pnl-bot"
LOCAL_ID = "999"          # 测试用的「本地已知条件单 id」


def _sync(root: Path, client, **kw):
    """测试用 sync：默认给一份含本地 id 的 `trades_log`、并关掉时间窗。

    归属上线后，没带 `text`（或 text 不命中本地 id）的记录会被判为未归属而排除 ——
    这是刻意的行为，所以这里统一补上最小可归属环境；要测排除逻辑的用例自己传参。

    **用 `if` 而不是 `setdefault`**：`setdefault(k, v)` 的 `v` 总会被求值，于是
    `_write_log` 会先跑一遍并**覆盖调用方刚写好的日志**（两者路径相同）。
    """
    if "trades_log" not in kw:
        kw["trades_log"] = _write_log(root, [
            {"steps": [{"detail": {"order": {"id": LOCAL_ID}}}]}])
    kw.setdefault("first_run_ts", 0)
    return sync(root, BOT, client, **kw)


def _write_log(root: Path, rows: list) -> Path:
    p = Path(root) / "logs" / "trades.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                 encoding="utf-8")
    return p


class TestLocalOrderIds(unittest.TestCase):
    """本地订单 id 索引 —— **四个容器都要收**。

    漏一个就让对应类型的单无法归属。实测教训：只收 `order` + `tp_orders`/`sl_orders`
    时本地 id 是 369 个，与交易所 `ao-<id>` 的**交集为 0**；补上 `tp_placed`（即
    `modify_tp_sl` 路径）后变成 681 个、交集 8 个。
    """

    @staticmethod
    def _log(root: Path, rows: list) -> Path:
        p = root / "logs" / "trades.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                     encoding="utf-8")
        return p

    def test_collects_all_containers(self):
        rows = [
            {"steps": [{"action": "open_short",
                        "detail": {"order": {"id": "o1"}}}]},
            {"steps": [{"action": "modify_tp_sl",
                        "detail": {"tp_placed": {"id": "t1"},
                                   "sl_placed": {"id": "s1"}}}]},
            {"steps": [{"action": "open_short",
                        "detail": {"tp_orders": [{"id": "t2"}],
                                   "sl_orders": [{"id": "s2"}]}}]},
        ]
        with tempfile.TemporaryDirectory() as td:
            got = local_order_ids(self._log(Path(td), rows))
            self.assertEqual(got, {"o1", "t1", "s1", "t2", "s2"})

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(local_order_ids(Path(td) / "nope.jsonl"), set())

    def test_corrupt_lines_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "logs" / "trades.jsonl"
            p.parent.mkdir(parents=True)
            p.write_text('{bad json\n{"steps":[{"detail":{"order":{"id":"ok"}}}]}\n',
                         encoding="utf-8")
            self.assertEqual(local_order_ids(p), {"ok"})

    def test_odd_shapes_do_not_raise(self):
        rows = [
            {"steps": None},
            {"steps": [None, "x", {"detail": None}, {"detail": {"order": "notdict"}}]},
            {"no_steps": 1},
        ]
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(local_order_ids(self._log(Path(td), rows)), set())


class TestIsOurs(unittest.TestCase):
    """归属判据：`ao-<id>` 且 id 在本地订单 id 里。"""

    def test_ao_hit(self):
        self.assertTrue(is_ours("ao-123", {"123"}))

    def test_ao_miss(self):
        self.assertFalse(is_ours("ao-999", {"123"}))

    def test_api_never_ours(self):
        """`api` / `-` 没有唯一性，认它们等于把别人的单也算进来。

        实测 `api` 用 (时间 ±180s, accum_size == |size|) 只能匹配 1/5，且
        `accum_size` 与单笔成交的 `size` 语义不同（35 vs 18）—— 模糊匹配不可靠。
        """
        self.assertFalse(is_ours("api", {"api"}))
        self.assertFalse(is_ours("-", {"-"}))
        self.assertFalse(is_ours("t-brk", {"t-brk"}))

    def test_empty_forms(self):
        self.assertFalse(is_ours(None, {"123"}))
        self.assertFalse(is_ours("", {"123"}))
        self.assertFalse(is_ours("ao-", {"123"}))
        self.assertFalse(is_ours("ao-  ", {"123"}))

    def test_empty_local_set(self):
        self.assertFalse(is_ours("ao-123", set()))


def _close(key: int, pnl: str, **kw) -> dict:
    """一条平仓记录（形状照抄真机返回的关键字段）。

    `text` 默认 `ao-<LOCAL_ID>` —— 归属判据只认这种形状，没有它一律判为未归属。
    """
    row = {"time_us": key, "pnl": pnl, "contract": "BTC_USDT", "side": "long",
           "text": f"ao-{LOCAL_ID}", "time": int(time.time()) + 60}
    row.update(kw)
    return row


class _CloseClient:
    def __init__(self, rows=None, fail=False):
        self.rows = list(rows or [])
        self.fail = fail
        self.calls: list[dict] = []

    def list_position_close(self, contract=None, limit=100):
        self.calls.append({"contract": contract, "limit": limit})
        if self.fail:
            raise RuntimeError("boom")
        return self.rows


class TestCloseKey(unittest.TestCase):
    """游标键：`position_close` 没有单调 id，只能用 time_us。"""

    def test_prefers_time_us(self):
        self.assertEqual(_close_key({"time_us": 1791302309972110, "time": 1791302309}),
                         1791302309972110)

    def test_falls_back_to_seconds_scaled(self):
        """只有 `time`（秒）时缩放到微秒量级，避免两种量级混进同一游标。"""
        self.assertEqual(_close_key({"time": 1791302309}), 1791302309000000)

    def test_missing_returns_none(self):
        self.assertIsNone(_close_key({}))

    def test_garbage_returns_none(self):
        self.assertIsNone(_close_key({"time_us": "abc"}))


class TestSync(unittest.TestCase):
    def test_first_sync_counts_closed_fills(self):
        rows = [
            _close(1001, "0"),        # 不赚不亏 —— 与 paper 的 != 0 口径一致，跳过
            _close(1002, "-1.5"),     # 止损
            _close(1003, "2.5"),      # 止盈
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = _sync(root, _CloseClient(rows))
            self.assertTrue(out["ok"])
            self.assertEqual(out["added"], 2)
            st = stats(root, BOT)
            self.assertEqual(st["trades"], 2)
            self.assertEqual(st["wins"], 1)
            self.assertAlmostEqual(st["pnl"], 1.0)
            self.assertAlmostEqual(st["worst"], -1.5)

    def test_idempotent_on_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = _CloseClient([_close(1002, "1.0")])
            _sync(root, c)
            out2 = _sync(root, c)
            self.assertEqual(out2["added"], 0, "重复同步不该重复计入")
            self.assertEqual(stats(root, BOT)["trades"], 1)

    def test_incremental_after_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _sync(root, _CloseClient([_close(1005, "1.0")]))
            out = _sync(root, _CloseClient([
                _close(1005, "1.0"), _close(1007, "-0.5")]))
            self.assertEqual(out["added"], 1)
            st = stats(root, BOT)
            self.assertEqual(st["trades"], 2)
            self.assertAlmostEqual(st["worst"], -0.5)

    def test_failure_does_not_advance_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = _sync(root, _CloseClient(fail=True))
            self.assertFalse(out["ok"])
            self.assertIn("error", out)
            self.assertEqual(load(root, BOT)["cursor"], "",
                             "拉取失败时推进游标会让那一段被永久跳过")
            self.assertIsNone(stats(root, BOT))

    def test_cursor_advances_even_when_all_skipped(self):
        """全是 pnl=0 时也要推游标，否则每次重复扫同一段。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = _sync(root, _CloseClient([_close(4242, "0")]))
            self.assertEqual(out["added"], 0)
            self.assertEqual(load(root, BOT)["cursor"], "4242")

    def test_rows_without_key_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = _sync(root, _CloseClient([{"pnl": "1.0"}, _close(9, "2.0")]))
            self.assertEqual(out["added"], 1)

    def test_worst_remembers_minimum_across_syncs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _sync(root, _CloseClient([_close(1001, "-3.0")]))
            _sync(root, _CloseClient([_close(1001, "-3.0"), _close(1002, "5.0")]))
            self.assertAlmostEqual(stats(root, BOT)["worst"], -3.0,
                                   msg="worst 应记住最差那笔，不被后续盈利覆盖")

    def test_all_wins_worst_is_smallest_win(self):
        """全盈利时 worst 是最小的**盈利**，不是 0。

        拿 0 顶替会读成「有一笔不赚不亏」，是假信息（与 `paper/store.py` 的
        `min(vals)` 口径一致）。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _sync(root, _CloseClient([_close(1001, "2.0"), _close(1002, "3.0")]))
            self.assertAlmostEqual(stats(root, BOT)["worst"], 2.0)

    def test_contracts_whitelist_filters_but_advances_cursor(self):
        """白名单外的成交不计入统计，但**游标要推进**（否则每次重扫同一段）。

        实测该账户的平仓历史里混着 `ETH_USDT` —— 全计入会让 brooks-btc 的
        「历史表现」失真。
        """
        rows = [
            _close(1001, "1.0", contract="BTC_USDT"),
            _close(1002, "9.0", contract="ETH_USDT"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = _sync(root, _CloseClient(rows), contracts=["BTC_USDT"])
            self.assertEqual(out["added"], 1)
            st = stats(root, BOT)
            self.assertEqual(st["trades"], 1)
            self.assertAlmostEqual(st["pnl"], 1.0)
            self.assertEqual(load(root, BOT)["cursor"], "1002",
                             "白名单外的成交也要推进游标")

    def test_no_whitelist_counts_everything(self):
        rows = [
            _close(1001, "1.0", contract="BTC_USDT"),
            _close(1002, "9.0", contract="ETH_USDT"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(_sync(root, _CloseClient(rows))["added"], 2)

    def test_fills_capped_but_totals_unaffected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [_close(1000 + i, "1.0") for i in range(1, KEEP_FILLS + 60)]
            _sync(root, _CloseClient(rows))
            rec = load(root, BOT)
            self.assertEqual(len(rec["fills"]), KEEP_FILLS, "明细应被截断")
            self.assertEqual(stats(root, BOT)["trades"], KEEP_FILLS + 59,
                             "统计走 totals，不该被明细上限影响")


class TestStats(unittest.TestCase):
    def test_empty_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(stats(Path(td), BOT), "没有任何平仓时应返回 None")

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
            _sync(root, _CloseClient([_close(1001, "-1.0")]))
            self.assertEqual(set(stats(root, BOT)), {"trades", "wins", "pnl", "worst"})


class TestListPositionCloseQuery(unittest.TestCase):
    def _client(self, seen: dict):
        from omnialpha.gate_client import GateClient

        c = GateClient.__new__(GateClient)

        def fake(method, path, qs, body=None):
            seen.update({"method": method, "path": path, "qs": qs})
            return []

        c.rest_signed_request = fake
        return c

    def test_hits_position_close_not_my_trades(self):
        """端点必须是 `position_close` —— `my_trades` 的返回里没有 pnl。"""
        seen: dict = {}
        self._client(seen).list_position_close(contract="BTC_USDT", limit=50)
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], "/api/v4/futures/usdt/position_close")
        self.assertIn("limit=50", seen["qs"])
        self.assertIn("contract=BTC_USDT", seen["qs"])

    def test_limit_clamped(self):
        seen: dict = {}
        self._client(seen).list_position_close(limit=99999)
        self.assertIn("limit=1000", seen["qs"])

    def test_optional_contract_omitted(self):
        seen: dict = {}
        self._client(seen).list_position_close()
        self.assertNotIn("contract", seen["qs"])


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
            _sync(root, _CloseClient([_close(1001, "-1.0"), _close(1002, "3.0")]))
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
            _sync(root, _CloseClient([_close(1001, "2.0"), _close(1002, "-1.0")]))
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
            # 归属判据需要本地订单 id 索引，先造一份 trades_log
            _write_log(root / "data" / "bots" / BOT, [
                {"steps": [{"detail": {"order": {"id": LOCAL_ID}}}]}])
            n = _exchange_pnl_sweep(
                self._bot(client=_CloseClient([_close(1001, "-1.0")])),
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
                self._bot(client=_CloseClient(fail=True)), self._paths(Path(td)))
            self.assertEqual(n, 0)


class TestAttributionInSync(unittest.TestCase):
    """sync 的三条新逻辑：归属过滤、时间窗、排除留痕。

    背景：原先只要 `contract ∈ bot.symbols` 就计入，于是画像混进了别的 bot
    （`app`/`t-drive`）、启动前历史（09-07~09-25），以及无法归属的 `api`。
    """

    T0 = 1790871600          # 基准时间
    LOG_ID = "2107498336587091968"

    def _trades_log(self, root: Path, rows: list) -> Path:
        p = root / "logs" / "trades.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                     encoding="utf-8")
        return p

    def _close(self, key: int, text: str, pnl: str, ts: int = None) -> dict:
        # 时间戳必须与 first_run_ts 同量级，否则会被时间窗先滤掉
        return {"time_us": key, "time": ts if ts is not None else self.T0 + 1,
                "text": text, "pnl": pnl, "contract": "BTC_USDT", "side": "long"}

    def test_attributed_counted_unattributed_skipped(self):
        rows = [
            self._close(1_000_000_000_000_000, f"ao-{self.LOG_ID}", "-1.5"),
            self._close(1_000_000_001_000_000, "api", "9.9"),
            self._close(1_000_000_002_000_000, "-", "9.9"),
            self._close(1_000_000_003_000_000, "ao-888", "9.9"),
            self._close(1_000_000_004_000_000, "t-drive", "9.9"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = self._trades_log(root, [
                {"steps": [{"detail": {"tp_placed": {"id": self.LOG_ID}}}]}])
            out = _sync(root, _CloseClient(rows), contracts=["BTC_USDT"],
                       trades_log=log, first_run_ts=self.T0)
            self.assertEqual(out["added"], 1, f"只有 ao-命中本地的那条该计入：{out}")
            self.assertEqual(out["skipped_unattributed"], 4, f"其余四条该被排除：{out}")
            self.assertEqual(stats(root, BOT)["trades"], 1)
            self.assertAlmostEqual(stats(root, BOT)["pnl"], -1.5)

    def test_before_first_run_excluded(self):
        rows = [
            self._close(1_000_000_000_000_000, f"ao-{self.LOG_ID}", "-1.5",
                        ts=self.T0 - 86400),
            self._close(1_000_000_001_000_000, f"ao-{self.LOG_ID}", "2.0",
                        ts=self.T0 + 60),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = self._trades_log(root, [
                {"steps": [{"detail": {"tp_placed": {"id": self.LOG_ID}}}]}])
            out = _sync(root, _CloseClient(rows), contracts=["BTC_USDT"],
                       trades_log=log, first_run_ts=self.T0)
            self.assertEqual(out["added"], 1, f"启动前那条该被排除：{out}")
            self.assertEqual(out["skipped_before_start"], 1)

    def test_excluded_list_recorded(self):
        rows = [self._close(1_000_000_000_000_000, "api", "-0.5")]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = _sync(root, _CloseClient(rows), contracts=["BTC_USDT"],
                       trades_log=Path(td) / "nope.jsonl", first_run_ts=self.T0)
            self.assertEqual(out["added"], 0)
            rec = load(root, BOT)
            self.assertEqual(len(rec["excluded"]), 1)
            self.assertEqual(rec["excluded"][0]["text"], "api")

    def test_active_close_from_trades_log(self):
        """bot 主动平仓走本地日志（executor 回传的 realized_pnl）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = self._trades_log(root, [
                {"ts": "2026-10-07T00:00:00+00:00", "plan_cycle": "c-1",
                 "steps": [{"action": "close", "detail": {"realized_pnl": -0.75}}]},
            ])
            out = _sync(root, _CloseClient([]), contracts=["BTC_USDT"],
                       trades_log=log, first_run_ts=0)
            self.assertEqual(out["added_active"], 1, f"主动平仓没被计入：{out}")
            self.assertAlmostEqual(stats(root, BOT)["pnl"], -0.75)

    def test_active_path_respects_time_window(self):
        """主动平仓来源也必须过 `first_run_ts`。

        原先它只按 `active_cursor` 过滤 —— 而本地 `trades.jsonl` 里可能有 bot 启动前的
        记录（实测 brooks-btc 的日志从 09-25 开始，而 `first_run_ts` 是 09-30），
        那些窗口前的主动平仓会被静默计入。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = self.T0 - 86400
            log = self._trades_log(root, [
                {"ts": old, "plan_cycle": "c-old",
                 "steps": [{"action": "close", "detail": {"realized_pnl": -99.0}}]},
                {"ts": self.T0 + 60, "plan_cycle": "c-new",
                 "steps": [{"action": "close", "detail": {"realized_pnl": -0.5}}]},
            ])
            out = _sync(root, _CloseClient([]), contracts=["BTC_USDT"],
                        trades_log=log, first_run_ts=self.T0)
            self.assertEqual(out["added_active"], 1, f"窗口前的主动平仓该被排除：{out}")
            self.assertAlmostEqual(stats(root, BOT)["pnl"], -0.5)

    def test_dedupe_prefers_position_close(self):
        """同一笔平仓两条路径都命中时只计一次。

        「天然互斥」只是今天的巧合（`close_position` 走普通下单 → text 是 `api`），
        不是任何地方保证的不变量。这里直接构造重叠：`position_close` 里是
        `ao-<本地id>`、本地日志里也有同一时刻的 `realized_pnl`。
        """
        ts = self.T0 + 60
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = self._trades_log(root, [
                {"steps": [{"detail": {"order": {"id": LOCAL_ID}}}]},
                {"ts": ts, "plan_cycle": "c-dup",
                 "steps": [{"action": "close", "detail": {"realized_pnl": -1.0}}]},
            ])
            rows = [self._close(1_000_000_000_000_000, f"ao-{LOCAL_ID}", "-1.0", ts=ts)]
            out = _sync(root, _CloseClient(rows), contracts=["BTC_USDT"],
                        trades_log=log, first_run_ts=self.T0)
            self.assertEqual(out["added"], 1, f"重复的平仓被计了两次：{out}")
            self.assertEqual(out["added_active"], 0)
            self.assertAlmostEqual(stats(root, BOT)["pnl"], -1.0)

    def test_excluded_keeps_newest_when_full(self):
        """`excluded` 满了之后要保留**最新**的，而不是冻结在最初那批。

        原先在 append 前判断长度，一旦满了就再也不追加 —— 数组永久冻结，
        `excluded` 作为「发现遗漏」的窗口就废了。
        """
        from omnialpha.memory.exchange_pnl import KEEP_EXCLUDED

        rows = [self._close(1_000_000_000_000_000 + i, "api", "-1.0")
                for i in range(KEEP_EXCLUDED + 20)]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _sync(root, _CloseClient(rows), contracts=["BTC_USDT"],
                  trades_log=Path(td) / "nope.jsonl", first_run_ts=self.T0)
            rec = load(root, BOT)
            self.assertEqual(len(rec["excluded"]), KEEP_EXCLUDED)
            self.assertEqual(rec["excluded"][-1]["time"],
                             rows[-1]["time"], "最新一条该在里面")

    def test_idempotent_with_attribution(self):
        rows = [self._close(1_000_000_000_000_000, f"ao-{self.LOG_ID}", "1.0")]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = self._trades_log(root, [
                {"steps": [{"detail": {"tp_placed": {"id": self.LOG_ID}}}]}])
            _sync(root, _CloseClient(rows), contracts=["BTC_USDT"],
                 trades_log=log, first_run_ts=self.T0)
            out2 = _sync(root, _CloseClient(rows), contracts=["BTC_USDT"],
                        trades_log=log, first_run_ts=self.T0)
            self.assertEqual(out2["added"], 0)
            self.assertEqual(stats(root, BOT)["trades"], 1)


class TestClosedCarriesNotifyFields(unittest.TestCase):
    """投影检测到的平仓要带足够字段，才能复用成交卡片逻辑发通知。

    背景：SL/TP 触发是**交易所侧**成交，没有 executor 的 `steps` —— 而
    `monitoring/notify.py::format_trade_card` 正是从 `steps` 生成卡片的，所以
    止盈/止损触发时**一条通知都没有**（改单、开仓都是本地发起，有 steps，正常）。
    实测 2026-10-07 用户反馈「没有止盈通知」。
    """

    def test_closed_has_entry_price_and_size(self):
        from omnialpha.memory.exchange_pnl import sync
        rows = [{
            "time_us": 1_000_000_000_000_000, "time": 1790871600 + 60,
            "text": f"ao-{LOCAL_ID}", "pnl": "1.5", "contract": "BTC_USDT",
            "side": "long", "long_price": "84000", "short_price": "0",
            "accum_size": "41",
        }]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = sync(root, BOT, _CloseClient(rows), contracts=["BTC_USDT"],
                       trades_log=_write_log(root, [
                           {"steps": [{"detail": {"order": {"id": LOCAL_ID}}}]}]),
                       first_run_ts=0)
            closed = out.get("closed") or []
            self.assertEqual(len(closed), 1, f"没返回可通知的平仓明细：{out}")
            f = closed[0]
            self.assertEqual(f["contract"], "BTC_USDT")
            self.assertEqual(f["side"], "long")
            self.assertEqual(f["entry_price"], "84000")
            self.assertEqual(f["size"], "41")
            self.assertAlmostEqual(f["pnl"], 1.5)

    def test_active_closes_not_in_closed(self):
        """主动平仓已由 executor 的 steps 通知，不能再发一次（会重复）。"""
        from omnialpha.memory.exchange_pnl import sync
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = _write_log(root, [
                {"ts": 1790871600 + 60, "plan_cycle": "c-1",
                 "steps": [{"action": "close", "detail": {"realized_pnl": -0.75}}]},
            ])
            out = sync(root, BOT, _CloseClient([]), contracts=["BTC_USDT"],
                       trades_log=log, first_run_ts=0)
            self.assertEqual(out.get("added_active"), 1)
            self.assertEqual(out.get("closed") or [], [],
                             "主动平仓不该进 closed（会与 steps 通知重复）")


class TestClosedToSteps(unittest.TestCase):
    """平仓明细 → `format_trade_card` 认得的 step 形状。"""

    def test_shapes_into_close_step(self):
        from omnialpha.watcher import _steps_from_closed
        from omnialpha.monitoring.notify import format_trade_card

        closed = [{"contract": "BTC_USDT", "side": "long", "pnl": 1.5,
                   "entry_price": "84000", "size": "41", "time": 1790871660}]
        steps = _steps_from_closed(closed)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["action"], "close")
        self.assertEqual(steps[0]["symbol"], "BTC_USDT")
        self.assertTrue(steps[0]["ok"])
        self.assertEqual(steps[0]["detail"]["realized_pnl"], 1.5)

        cards = format_trade_card(BOT, steps)
        self.assertEqual(len(cards), 1, "卡片没生成 —— 形状对不上 format_trade_card")
        title = cards[0]["header"]["title"]["content"]
        self.assertIn("止盈", title, f"正盈亏该判为止盈，实际标题：{title}")

    def test_time_comes_from_the_close_not_render_time(self):
        """时间必须取平仓时刻 —— 放错层级会 fallback 成「当前时间」。

        `notify._step_time` 依次读 `detail.order.create_time` → `detail.create_time`
        → `s.create_time` → `s.ts`。把 ts 放进 `detail` 读不到，卡片上显示的就是
        渲染时刻（实测 2026-10-07 卡片写着 22:50，而平仓发生在 13:38）。
        """
        from omnialpha.watcher import _steps_from_closed
        from omnialpha.monitoring.notify import format_trade_card

        close_ts = 1791380321
        steps = _steps_from_closed([{"contract": "BTC_USDT", "side": "long",
                                     "pnl": 1.0, "entry_price": "84000",
                                     "size": "10", "time": close_ts}])
        self.assertEqual(steps[0].get("ts"), close_ts, "ts 该在 step 层")
        card = format_trade_card(BOT, steps)[0]
        txt = " ".join(
            f["text"]["content"]
            for el in (card.get("elements") or [])
            for f in (el.get("fields") or [])
        )
        expect = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(close_ts))
        self.assertIn(expect[:13], txt, f"卡片时间不是平仓时刻：{txt}")

    def test_negative_pnl_is_stop_loss(self):
        from omnialpha.watcher import _steps_from_closed
        from omnialpha.monitoring.notify import format_trade_card

        steps = _steps_from_closed([{"contract": "BTC_USDT", "side": "short",
                                     "pnl": -2.0, "entry_price": "84000",
                                     "size": "10", "time": 1790871660}])
        title = format_trade_card(BOT, steps)[0]["header"]["title"]["content"]
        self.assertIn("止损", title)

    def test_empty_is_safe(self):
        from omnialpha.watcher import _steps_from_closed
        self.assertEqual(_steps_from_closed([]), [])


class TestPnlSweepHasOwnCadence(unittest.TestCase):
    """平仓检测不能跟 300s 的保护单 sweep 同频。

    用户反馈「300 秒太长了」：止盈触发了最多 5 分钟后才收到通知。而它只读交易所的
    平仓历史，与保护单对账毫无关系 —— 没有理由共享节奏。
    """

    def test_signature_exposes_pnl_sweep_sec(self):
        import inspect
        from omnialpha.watcher import run_forever

        sig = inspect.signature(run_forever)
        self.assertIn("pnl_sweep_sec", sig.parameters)
        self.assertLess(float(sig.parameters["pnl_sweep_sec"].default), 300.0,
                        "平仓检测的默认周期不该是 300s")

    def test_called_under_do_pnl_not_do_sweep(self):
        """源码级回归钉：`_exchange_pnl_sweep` 必须挂在 `do_pnl` 分支里。

        （`run_forever` 是无限循环，没法直接跑；用源码位置钉住结构。）
        """
        import inspect
        from omnialpha.watcher import run_forever

        src = inspect.getsource(run_forever)
        i = src.index("_exchange_pnl_sweep")
        head = src[:i]
        self.assertIn("if do_pnl:", head, "平仓检测没挂在 do_pnl 上")
        self.assertNotIn(
            "if do_sweep:", head,
            "平仓检测被放回 do_sweep 块了（那样又变回 300s 延迟）")


if __name__ == "__main__":
    unittest.main()
