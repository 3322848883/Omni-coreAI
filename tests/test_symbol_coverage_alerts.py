# -*- coding: utf-8 -*-
"""T16 残余：**按币**零数据告警（data_monitor）+ 多所品种从配置读（watchdog）+ coin_info 推导。

已落地的 T16 部分（见 `tests/test_aux_watchlist_contracts.py`）覆盖了 `fetch_aux.CONTRACTS`
从 watchlist 读与 `aux_monitor` 的按币覆盖。这里补三处**同一病因**的残留
（spec §S2.4⑪「新增币后 aux 工具不再静默空」）：

1. `data_monitor.DataMonitor` 只跟踪**见过的** key —— 一个从来没产出过数据的币
   （新加的币、在该所没上线的币）**永远不会被报出来**：没有 key 就没有循环，
   没有循环就没有告警。整体 ok 掩盖「这个币零数据」（审计 A-10）。
2. `watchdog.py` 的多所采集品种是**代码里的硬编码清单**（`--symbols "BTC_USDT,..."`）：
   加一个币要改代码，而漏改的那处不报错、只是永远没数据。
3. `fetch_aux.COIN_INFO_SYMBOLS` 是写死的 `["BTC","ETH","SOL"]`：新加的加密币
   永远没有币种基本面数据（同类静默空，只是换了个接口）。

三处都遵循同一条原则：**「不知道」不等于「没有」，且都要说出来**。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PA = ROOT / "pa-data-source"
sys.path.insert(0, str(PA))

import data_monitor as dm  # noqa: E402
import watchdog  # noqa: E402

TODAYS_MULTI = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "XRP_USDT"]


def _write_watchlist(td, body: str) -> Path:
    p = Path(td) / "watchlist.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _load_fetch_aux_with_watchlist(path) -> object:
    """在指定 watchlist 下重新加载 `fetch_aux`，返回模块（读它的派生常量）。"""
    old = os.environ.get("OMNIALPHA_WATCHLIST")
    os.environ["OMNIALPHA_WATCHLIST"] = str(path)
    try:
        sys.modules.pop("fetch_aux", None)
        return importlib.import_module("fetch_aux")
    finally:
        if old is None:
            os.environ.pop("OMNIALPHA_WATCHLIST", None)
        else:
            os.environ["OMNIALPHA_WATCHLIST"] = old
        sys.modules.pop("fetch_aux", None)


class TestDataMonitorPerSymbol(unittest.TestCase):
    """「某币零数据」必须能报出来 —— 这是 aux 静默空在监控侧的可观测面。"""

    def test_zero_data_symbol_is_reported(self):
        m = dm.DataMonitor(missing_grace_sec=0)
        m.expect(["BTC_USDT", "DOGE_USDT"], source="test")
        m.update("BTC_USDT", "1h")
        self.assertEqual(m.missing_symbols(), ["DOGE_USDT"])
        alerts = m.log_missing_alerts()
        self.assertEqual([a["symbol"] for a in alerts], ["DOGE_USDT"])

    def test_repeated_checks_do_not_spam(self):
        m = dm.DataMonitor(missing_grace_sec=0)
        m.expect(["BTC_USDT", "DOGE_USDT"])
        m.update("BTC_USDT", "1h")
        self.assertEqual(len(m.log_missing_alerts()), 1)
        self.assertEqual(m.log_missing_alerts(), [], "同一品种只报一次")

    def test_alert_rearms_after_data_then_loss(self):
        m = dm.DataMonitor(missing_grace_sec=0)
        m.expect(["BTC_USDT", "DOGE_USDT"])
        m.update("BTC_USDT", "1h")
        self.assertEqual(len(m.log_missing_alerts()), 1)
        m.update("DOGE_USDT", "1h")           # 数据来了
        self.assertEqual(m.missing_symbols(), [])
        m.reset_symbol("DOGE_USDT")           # 又断了
        self.assertEqual(len(m.log_missing_alerts()), 1, "再次零数据要重新报")

    def test_grace_period_suppresses_startup_false_positive(self):
        """刚启动时数据还没到，别把「还没开始采」报成「这个币零数据」。"""
        m = dm.DataMonitor(missing_grace_sec=3600)
        m.expect(["BTC_USDT", "DOGE_USDT"])
        self.assertEqual(m.missing_symbols(), [])

    def test_no_expectation_means_no_new_alerts(self):
        """没声明期望宇宙 → 行为与改动前完全一致（不新增任何告警）。"""
        m = dm.DataMonitor(missing_grace_sec=0)
        m.update("BTC_USDT", "1h")
        self.assertEqual(m.missing_symbols(), [])
        self.assertEqual(m.log_missing_alerts(), [])

    def test_expected_universe_from_watchlist(self):
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols:\n"
                                      "  - name: BTC_USDT\n"
                                      "    intervals: [1h]\n"
                                      "  - name: DOGE_USDT\n"
                                      "    intervals: [1h, 4h]\n")
            m = dm.DataMonitor(missing_grace_sec=0)
            self.assertTrue(m.ensure_expected(path=wl))
            self.assertEqual(m.expected_symbols(), ["BTC_USDT", "DOGE_USDT"])
            m.update("BTC_USDT", "1h")
            alerts = m.log_missing_alerts()
            self.assertEqual([a["symbol"] for a in alerts], ["DOGE_USDT"])
            self.assertEqual(alerts[0]["intervals"], ["1h", "4h"])

    def test_broken_watchlist_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols: [oops\n:::")
            m = dm.DataMonitor(missing_grace_sec=0)
            self.assertFalse(m.ensure_expected(path=wl))
            self.assertEqual(m.missing_symbols(), [])

    def test_watchlist_without_symbols_is_not_an_expectation(self):
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "market_type: futures\n")
            m = dm.DataMonitor(missing_grace_sec=0)
            self.assertFalse(m.ensure_expected(path=wl))

    def test_missing_watchlist_is_not_an_expectation(self):
        m = dm.DataMonitor(missing_grace_sec=0)
        self.assertFalse(m.ensure_expected(path=Path("no-such-dir") / "watchlist.yaml"))

    def test_explicit_expect_wins_over_watchlist(self):
        m = dm.DataMonitor(missing_grace_sec=0)
        m.expect(["ONLY_USDT"], source="explicit")
        self.assertEqual(m.expected_symbols(), ["ONLY_USDT"])

    def test_broken_watchlist_is_not_reread_every_check(self):
        """读不到就别每 60s 重读+重告警一次（check_and_alert 是常驻循环）。"""
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols: [oops\n:::")
            m = dm.DataMonitor(missing_grace_sec=0)
            self.assertFalse(m.ensure_expected(path=wl))
            Path(wl).write_text("symbols:\n  - name: BTC_USDT\n", encoding="utf-8")
            self.assertFalse(m.ensure_expected(path=wl), "失败后的重试窗口内不再重读")


class TestFetchAuxCoinInfoDerivation(unittest.TestCase):
    """coin_info 品种从 CONTRACTS 推导：新加的加密币自动纳入，贵金属排除。"""

    def test_today_default_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols:\n"
                                      "  - name: BTC_USDT\n"
                                      "  - name: ETH_USDT\n"
                                      "  - name: SOL_USDT\n"
                                      "  - name: XAU_USDT\n"
                                      "  - name: XAG_USDT\n")
            mod = _load_fetch_aux_with_watchlist(wl)
            self.assertEqual(mod.COIN_INFO_SYMBOLS, ["BTC", "ETH", "SOL"])

    def test_new_crypto_coin_is_included(self):
        """watchlist 加 DOGE → coin_info 也采它（写死清单下这里必失败）。"""
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols:\n"
                                      "  - name: BTC_USDT\n"
                                      "  - name: DOGE_USDT\n")
            mod = _load_fetch_aux_with_watchlist(wl)
            self.assertEqual(mod.COIN_INFO_SYMBOLS, ["BTC", "DOGE"])

    def test_metals_are_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols:\n"
                                      "  - name: XAU_USDT\n"
                                      "  - name: XAG_USDT\n")
            mod = _load_fetch_aux_with_watchlist(wl)
            self.assertEqual(mod.COIN_INFO_SYMBOLS, [])

    def test_onchain_tokens_stays_declared(self):
        """链上 token 不可推导（token 参数只对 EVM 链生效）→ 保留常量。"""
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols:\n  - name: DOGE_USDT\n")
            mod = _load_fetch_aux_with_watchlist(wl)
            self.assertEqual(mod.ONCHAIN_TOKENS, ["ETH"])


class TestWatchdogMultiSymbols(unittest.TestCase):
    """多所采集品种从 watchlist.yaml 读（旧实现是代码里的硬编码清单）。"""

    def test_symbols_come_from_config(self):
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols:\n"
                                      "  - name: BTC_USDT\n"
                                      "multi_venue:\n"
                                      "  symbols: [BTC_USDT, NEW_USDT]\n"
                                      "  intervals: [1h, 4h]\n")
            cfg = watchdog.load_watchlist_config(wl)
            self.assertEqual(watchdog.resolve_multi_symbols(cfg),
                             ["BTC_USDT", "NEW_USDT"])
            self.assertEqual(watchdog.resolve_multi_intervals(cfg), ["1h", "4h"])

    def test_missing_watchlist_falls_back_to_todays_list(self):
        cfg = watchdog.load_watchlist_config(Path("no-such-dir") / "watchlist.yaml")
        self.assertEqual(watchdog.resolve_multi_symbols(cfg), TODAYS_MULTI)

    def test_broken_watchlist_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "multi_venue: [not, a, mapping\n")
            cfg = watchdog.load_watchlist_config(wl)
            self.assertEqual(watchdog.resolve_multi_symbols(cfg), TODAYS_MULTI)

    def test_config_without_multi_venue_uses_todays_list(self):
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols:\n  - name: BTC_USDT\n")
            cfg = watchdog.load_watchlist_config(wl)
            self.assertEqual(watchdog.resolve_multi_symbols(cfg), TODAYS_MULTI)


class TestWatchdogVenueValidation(unittest.TestCase):
    """逐所启动校验：确认没有 → 告警并跳过；未校验 → 如实说「不知道」。"""

    def _patch_listing(self, mapping):
        old = watchdog.venue_listing
        watchdog.venue_listing = lambda ex: mapping.get(ex)
        self.addCleanup(setattr, watchdog, "venue_listing", old)

    def test_confirmed_missing_everywhere_is_dropped(self):
        self._patch_listing({"bitget": {"BTC_USDT"},
                             "hyperliquid": {"BTC_USDT"}})
        rep = watchdog.validate_multi_symbols(["BTC_USDT", "XAU_USDT"],
                                              exchanges=["bitget", "hyperliquid"])
        self.assertEqual(rep["kept"], ["BTC_USDT"])
        self.assertEqual([d["symbol"] for d in rep["dropped"]], ["XAU_USDT"])
        self.assertEqual(rep["confirmed_missing"]["bitget"], ["XAU_USDT"])

    def test_missing_on_one_venue_only_is_kept(self):
        """单所缺币是**上币差异**（换所就行），不该把币从全局摘掉。"""
        self._patch_listing({"bitget": {"BTC_USDT", "TON_USDT"},
                             "hyperliquid": {"BTC_USDT"}})
        rep = watchdog.validate_multi_symbols(["BTC_USDT", "TON_USDT"],
                                              exchanges=["bitget", "hyperliquid"])
        self.assertEqual(rep["kept"], ["BTC_USDT", "TON_USDT"])
        self.assertEqual(rep["dropped"], [])
        self.assertEqual(rep["confirmed_missing"]["hyperliquid"], ["TON_USDT"])

    def test_unverified_venues_are_reported_not_guessed(self):
        self._patch_listing({"bitget": {"BTC_USDT"}})
        rep = watchdog.validate_multi_symbols(["BTC_USDT"], exchanges=["bitget", "okx"])
        self.assertEqual(rep["checked"], ["bitget"])
        self.assertEqual(rep["unverified"], ["okx"])
        self.assertEqual(rep["kept"], ["BTC_USDT"])

    def test_all_venues_unverified_keeps_declared_list(self):
        """离线/取不到 → 一个都不剔（保持改动前的清单，不拿「不知道」当「没有」）。"""
        self._patch_listing({})
        syms = list(TODAYS_MULTI)
        rep = watchdog.validate_multi_symbols(syms, exchanges=["bitget", "hyperliquid"])
        self.assertEqual(rep["kept"], syms)
        self.assertEqual(rep["dropped"], [])
        self.assertEqual(rep["unverified"], ["bitget", "hyperliquid"])

    def test_kline_multi_args_match_todays_invocation(self):
        """参数形状与改动前**逐字一致**（品种来自配置，不是代码）。"""
        self._patch_listing({"bitget": set(TODAYS_MULTI), "hyperliquid": set(TODAYS_MULTI)})
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "symbols:\n  - name: BTC_USDT\n"
                                      "multi_venue:\n"
                                      "  symbols: [BTC_USDT, ETH_USDT, SOL_USDT, DOGE_USDT, XRP_USDT]\n"
                                      "  intervals: [1m, 5m, 15m, 1h, 4h, 1d]\n")
            cfg = watchdog.load_watchlist_config(wl)
            args = watchdog.kline_multi_args(cfg, exchanges=["bitget", "hyperliquid"])
            self.assertEqual(args, ["--poll", "30",
                                    "--symbols", ",".join(TODAYS_MULTI),
                                    "--intervals", "1m,5m,15m,1h,4h,1d"])

    def test_kline_multi_args_offline_is_unchanged(self):
        self._patch_listing({})
        with tempfile.TemporaryDirectory() as td:
            wl = _write_watchlist(td, "multi_venue:\n"
                                      "  symbols: [BTC_USDT, DOGE_USDT]\n")
            cfg = watchdog.load_watchlist_config(wl)
            args = watchdog.kline_multi_args(cfg, exchanges=["bitget"])
            self.assertEqual(args[3], "BTC_USDT,DOGE_USDT")


class TestWatchdogTargetsUseConfig(unittest.TestCase):
    def test_targets_are_resolved_at_startup(self):
        """TARGETS 里的 kline-multi 参数在启动时解析（import 时不发请求）。"""
        target = [t for t in watchdog.TARGETS if t["name"] == "kline-multi"]
        self.assertEqual(len(target), 1)
        self.assertIsNone(target[0]["args"], "启动前不解析（import 不得发网络请求）")


if __name__ == "__main__":
    unittest.main()
