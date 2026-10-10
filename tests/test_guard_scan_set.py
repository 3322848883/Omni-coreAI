# -*- coding: utf-8 -*-
"""守护扫描驱动源 = `bot.symbols` ∪ 「交易所上本 bot 有归属痕迹的合约」（spec T10 / D6）。

回归背景：5 个 sweep 原先一律 `for sym in bot.symbols` —— **从 yaml 里删掉一个币，
它上面的存量仓位立刻脱离全部守护**（裸仓），而「删旧币、加新币」正是换标的的核心
场景。覆盖面不该由「配置里还写不写它」决定。

反方向的约束同样重要：账户是**多 bot 共用**的，按「账户里有仓」扩展会把别的 bot 的
仓位拉进本 bot 的扫描集合 → 替他 bot 补 SL / 上移 SL（错币操作）。所以归属判据取
**痕迹**（订单 text 前缀 `t-<label_prefix>`），取不到就不扫。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from omnialpha.watcher import (
    ProjectPaths,
    _SCAN_SYMBOL_CAP,
    _text_owned,
    guard_scan_set,
    record_guard_coverage,
)


class FakeClient:
    """最小交易所桩：账户级持仓 + 挂单 + 条件单。"""

    def __init__(self, positions=None, orders=None, price_orders=None,
                 fail_positions=False):
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.price_orders = list(price_orders or [])
        self.fail_positions = fail_positions

    def get_positions(self):
        if self.fail_positions:
            raise RuntimeError("positions unavailable")
        return [dict(p) for p in self.positions]

    def list_orders(self):
        return [dict(o) for o in self.orders]

    def list_price_orders(self):
        return [dict(o) for o in self.price_orders]


def _bot(symbols, label_prefix="brooks", bot_id="guard-bot"):
    from omnialpha.config import BotConfig

    return BotConfig(bot_id=bot_id, env="paper", symbols=list(symbols),
                     label_prefix=label_prefix)


def _position(contract, size=5):
    return {"contract": contract, "size": size, "entry_price": 100.0}


def _price_order(contract, text, size=-5):
    return {"id": 1, "status": "open",
            "initial": {"contract": contract, "text": text, "size": size,
                        "is_reduce_only": True},
            "trigger": {"price": 90.0}}


def _open_order(contract, text, size=5):
    return {"id": 2, "status": "open", "contract": contract,
            "text": text, "size": size, "left": size, "is_reduce_only": False}


class TestTextOwned(unittest.TestCase):
    """归属判据与 `executor._text_owned` 同规则（segment-safe + fail-closed）。"""

    def test_segment_safe(self):
        self.assertTrue(_text_owned("t-brooks", "brooks"))
        self.assertTrue(_text_owned("t-brooks-ls", "brooks"))
        # `t-brooksXX` 不是我的（前缀必须到分隔符为止）
        self.assertFalse(_text_owned("t-brooksXX", "brooks"))
        self.assertFalse(_text_owned("t-other-ls", "brooks"))

    def test_empty_label_is_fail_closed(self):
        """没配 label_prefix → 不能退化成「整个账户都是我的」。"""
        self.assertFalse(_text_owned("t-anything", ""))
        self.assertFalse(_text_owned("", ""))


class TestScanSetCoversOwnedPositions(unittest.TestCase):
    def test_single_symbol_without_extra_traces_is_unchanged(self):
        """单币、账户上无额外归属合约 → 扫描集合 == bot.symbols（逐字不变）。"""
        scan = guard_scan_set(_bot(["BTC_USDT"]), FakeClient())
        self.assertEqual(scan["symbols"], ["BTC_USDT"])
        self.assertEqual(scan["extra"], [])
        self.assertEqual(scan["skipped"], [])

    def test_removed_symbol_position_still_scanned(self):
        """(a) yaml 里删掉了 ETH，但账户上有本 bot 归属的 ETH 仓 → 仍被扫描覆盖。

        旧实现（`for sym in bot.symbols`）下 `ETH_USDT` 一次都不会被扫到。
        """
        bot = _bot(["BTC_USDT"])
        client = FakeClient(
            positions=[_position("ETH_USDT")],
            price_orders=[_price_order("ETH_USDT", "t-brooks-ls")],
        )
        scan = guard_scan_set(bot, client)
        self.assertIn("ETH_USDT", scan["symbols"], "删币后存量仓位脱离了守护")
        self.assertEqual(scan["extra"], ["ETH_USDT"])
        self.assertEqual(scan["symbols"], ["BTC_USDT", "ETH_USDT"])

    def test_owned_open_order_also_counts(self):
        """未成交的入场单同样是归属痕迹（阶梯挂单场景：有单无仓）。"""
        scan = guard_scan_set(_bot(["BTC_USDT"]), FakeClient(
            orders=[_open_order("SOL_USDT", "t-brooks-lp")]))
        self.assertIn("SOL_USDT", scan["symbols"])

    def test_other_bots_position_is_not_pulled_in(self):
        """(b) 他 bot 归属的仓**不会**被拉进扫描集合。"""
        bot = _bot(["BTC_USDT"], label_prefix="brooks")
        client = FakeClient(
            positions=[_position("SOL_USDT")],
            price_orders=[_price_order("SOL_USDT", "t-ladder-ls")],
            orders=[_open_order("SOL_USDT", "t-ladder-lp")],
        )
        scan = guard_scan_set(bot, client)
        self.assertEqual(scan["symbols"], ["BTC_USDT"])
        self.assertNotIn("SOL_USDT", scan["symbols"])
        # 有仓却不扫 → 必须能回答「为什么没守护它」
        self.assertIn({"symbol": "SOL_USDT", "reason": "not_owned"}, scan["skipped"])

    def test_naked_position_without_any_trace_is_not_guessed(self):
        """账户上有仓、但**任何** bot 都没留痕迹 → 判不了归属，不猜（不扫）。"""
        client = FakeClient(positions=[_position("SOL_USDT")])
        scan = guard_scan_set(_bot(["BTC_USDT"]), client)
        self.assertEqual(scan["symbols"], ["BTC_USDT"])
        self.assertEqual(scan["skipped"],
                         [{"symbol": "SOL_USDT", "reason": "not_owned"}])

    def test_no_label_prefix_never_expands(self):
        """没配 label_prefix（且无 bot_id 兜底）→ 不扩展。"""
        bot = _bot(["BTC_USDT"], label_prefix="", bot_id="")
        client = FakeClient(positions=[_position("ETH_USDT")],
                            price_orders=[_price_order("ETH_USDT", "t-x-ls")])
        scan = guard_scan_set(bot, client)
        self.assertEqual(scan["symbols"], ["BTC_USDT"])

    def test_symbol_cap_bounds_scan_and_marks_over_cap(self):
        """币数上限：超限只扫前 N 个，其余标 `over_cap`（成本有界）。"""
        extra = [f"C{i}_USDT" for i in range(_SCAN_SYMBOL_CAP + 3)]
        client = FakeClient(price_orders=[_price_order(s, f"t-brooks-ls") for s in extra])
        with self.assertLogs("omnialpha.watcher", level="WARNING") as cm:
            scan = guard_scan_set(_bot(["BTC_USDT"]), client)
        self.assertEqual(len(scan["symbols"]), _SCAN_SYMBOL_CAP)
        self.assertEqual(scan["symbols"][0], "BTC_USDT")
        over = [s for s in scan["skipped"] if s["reason"] == "over_cap"]
        self.assertEqual(len(over), (1 + len(extra)) - _SCAN_SYMBOL_CAP)
        self.assertEqual([s["symbol"] for s in over], extra[_SCAN_SYMBOL_CAP - 1:])
        self.assertTrue(any("超过上限" in m for m in cm.output), cm.output)

    def test_snapshot_failure_is_recorded_not_fatal(self):
        """取不到持仓 → 不扩展、不留痕失败原因（而不是静默当没有）。"""
        scan = guard_scan_set(_bot(["BTC_USDT"]), FakeClient(fail_positions=True))
        self.assertEqual(scan["symbols"], ["BTC_USDT"])
        self.assertTrue(any("positions:" in e for e in scan["errors"]), scan["errors"])

    def test_no_client_returns_universe_only(self):
        """建不出 client 时退化成 `bot.symbols`（不抛、不扩展）。"""
        scan = guard_scan_set(_bot(["BTC_USDT"]), None)
        self.assertEqual(scan["symbols"], ["BTC_USDT"])
        self.assertEqual(scan["extra"], [])


class TestSweepsUseScanSet(unittest.TestCase):
    """5 个 sweep 真的按扫描集合迭代（而不是各自的 `bot.symbols`）。"""

    def _paths(self, td):
        return ProjectPaths(root=Path(td))

    def _run(self, fn, bot, paths, client, symbols):
        from omnialpha.executor import Executor

        seen: list = []
        orig = Executor.ensure_protection
        Executor.ensure_protection = (
            lambda self, sym, **kw: (seen.append(sym), {"skipped": "test"})[1])
        try:
            fn(bot, paths, {}, client=client, symbols=symbols)
        finally:
            Executor.ensure_protection = orig
        return seen

    def test_auto_protect_sweep_covers_removed_symbol(self):
        from omnialpha.watcher import _auto_protect_sweep

        with tempfile.TemporaryDirectory() as td:
            bot = _bot(["BTC_USDT"])
            client = FakeClient(positions=[_position("ETH_USDT")],
                                price_orders=[_price_order("ETH_USDT", "t-brooks-ls")])
            scan = guard_scan_set(bot, client)
            seen = self._run(_auto_protect_sweep, bot, self._paths(td), client,
                             scan["symbols"])
            self.assertEqual(seen, ["BTC_USDT", "ETH_USDT"])

    def test_auto_protect_sweep_computes_scan_set_when_not_given(self):
        """单独调用（未传 symbols）时也要自己算出含归属合约的集合。"""
        from omnialpha.watcher import _auto_protect_sweep

        with tempfile.TemporaryDirectory() as td:
            bot = _bot(["BTC_USDT"])
            client = FakeClient(positions=[_position("ETH_USDT")],
                                price_orders=[_price_order("ETH_USDT", "t-brooks-ls")])
            seen = self._run(_auto_protect_sweep, bot, self._paths(td), client, None)
            self.assertEqual(seen, ["BTC_USDT", "ETH_USDT"])


class TestGuardCoverageRecorded(unittest.TestCase):
    """每轮扫描落 `covered`/`skipped`：事后能回答「这个仓到底有没有被守护」。"""

    def test_writes_covered_and_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            paths = ProjectPaths(root=Path(td))
            bot = _bot(["BTC_USDT"])
            client = FakeClient(positions=[_position("ETH_USDT"), _position("XRP_USDT")],
                                price_orders=[_price_order("ETH_USDT", "t-brooks-ls")])
            scan = guard_scan_set(bot, client)
            record_guard_coverage(paths, bot.bot_id, "round", scan)
            p = paths.bot_paths(bot.bot_id).state / "guard_coverage.json"
            rec = json.loads(p.read_text(encoding="utf-8"))
            sec = rec["rounds"]["round"]
            self.assertEqual(sec["covered"], ["BTC_USDT", "ETH_USDT"])
            self.assertEqual(sec["covered_extra"], ["ETH_USDT"])
            self.assertEqual(sec["counts"],
                             {"covered": 2, "skipped": 1, "extra": 1})
            self.assertEqual(sec["skipped"],
                             [{"symbol": "XRP_USDT", "reason": "not_owned"}])
            self.assertEqual(sec["positions"], ["ETH_USDT", "XRP_USDT"])
            self.assertEqual(rec["cap"], _SCAN_SYMBOL_CAP)

    def test_second_sweep_section_does_not_clobber_first(self):
        """60s 的 give_back 与 300s 的 round 分节共存（各自可读）。"""
        with tempfile.TemporaryDirectory() as td:
            paths = ProjectPaths(root=Path(td))
            bot = _bot(["BTC_USDT"])
            scan = guard_scan_set(bot, FakeClient())
            record_guard_coverage(paths, bot.bot_id, "round", scan)
            record_guard_coverage(paths, bot.bot_id, "give_back", scan)
            p = paths.bot_paths(bot.bot_id).state / "guard_coverage.json"
            rounds = json.loads(p.read_text(encoding="utf-8"))["rounds"]
            self.assertEqual(sorted(rounds), ["give_back", "round"])

    def test_corrupt_file_does_not_block_write(self):
        with tempfile.TemporaryDirectory() as td:
            paths = ProjectPaths(root=Path(td))
            bot = _bot(["BTC_USDT"])
            p = paths.bot_paths(bot.bot_id).state / "guard_coverage.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{not json", encoding="utf-8")
            record_guard_coverage(paths, bot.bot_id, "round",
                                  guard_scan_set(bot, FakeClient()))
            rec = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(rec["rounds"]["round"]["covered"], ["BTC_USDT"])


if __name__ == "__main__":
    unittest.main()
