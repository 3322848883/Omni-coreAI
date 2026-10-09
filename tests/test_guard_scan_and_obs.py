# -*- coding: utf-8 -*-
"""T10：守护驱动源（含「有归属的合约」）+ 观测落盘（tool_usage 按币 / charts 元数据）。

覆盖 spec `symbol-as-parameter.md` §S2.4⑦⑨：

- 扫描集合 = `bot.symbols` ∪「交易所上本 bot 有归属痕迹的合约」——
  **从 yaml 删掉一个币，它的存量仓位不能就此脱离守护**（那就成了裸仓）。
- 他 bot 归属的合约**不被拉进来**（否则会动别人的仓）。
- 单币且账户上没有额外归属合约时，扫描集合逐字等于 `bot.symbols`（I11）。
- 币数超上限时截断并留 `over_cap` 痕（成本有界，I10）。
- `tool_usage` 每轮清零；`_tool_usage_summary` 按币归因
  （`by_symbol` / `missing_symbol` / `out_of_universe`），旧字段逐字不变。
- `charts` 元数据落 `thinking.json`，且**不含 base64 本体**（会撑爆文件）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha import watcher as W  # noqa: E402
from omnialpha.config import BotConfig  # noqa: E402
from omnialpha.strategist.loop import PlanRunner, StrategistConfig  # noqa: E402
from omnialpha.watcher import guard_scan_set  # noqa: E402


class _StubClient:
    """只回答三件事：挂单、条件单、持仓（归属判据就靠这三种）。"""

    def __init__(self, orders=None, price_orders=None, positions=None):
        self._orders = list(orders or [])
        self._price_orders = list(price_orders or [])
        self._positions = list(positions or [])

    def list_orders(self, contract=None):
        return [dict(o) for o in self._orders]

    def list_price_orders(self, contract=None):
        return [dict(p) for p in self._price_orders]

    def get_positions(self):
        return [dict(p) for p in self._positions]


def _bot(symbols, label_prefix="brk") -> BotConfig:
    return BotConfig(bot_id="b1", symbols=list(symbols), label_prefix=label_prefix)


class TestGuardScanSet(unittest.TestCase):
    def test_owned_symbol_outside_yaml_is_still_scanned(self):
        """T10 的核心：yaml 里删了 ETH，但账户上还有本 bot 的 ETH 仓 → 仍要守护它。"""
        scan = guard_scan_set(
            _bot(["BTC_USDT"]),
            _StubClient(orders=[{"contract": "ETH_USDT", "text": "t-brk"}],
                        positions=[{"contract": "ETH_USDT", "size": 2}]),
        )
        self.assertIn("ETH_USDT", scan["symbols"],
                      "有归属痕迹的合约必须继续被扫（否则删币即裸仓）")
        self.assertEqual(scan["extra"], ["ETH_USDT"])
        self.assertEqual(scan["symbols"][0], "BTC_USDT", "yaml 里的币排在前面")

    def test_owned_symbol_via_price_order_only(self):
        """归属痕迹也可能只在**条件单**上（挂着的 SL）。"""
        scan = guard_scan_set(
            _bot(["BTC_USDT"]),
            _StubClient(price_orders=[{"contract": "SOL_USDT",
                                       "initial": {"text": "t-brk-sl"}}]),
        )
        self.assertIn("SOL_USDT", scan["symbols"])

    def test_other_bots_symbol_is_not_scanned(self):
        """他 bot 归属的合约不能被拉进来 —— 那会去动别人的仓。"""
        scan = guard_scan_set(
            _bot(["BTC_USDT"]),
            _StubClient(orders=[{"contract": "ETH_USDT", "text": "t-other"}],
                        positions=[{"contract": "ETH_USDT", "size": 2}]),
        )
        self.assertNotIn("ETH_USDT", scan["symbols"])
        self.assertEqual([s["symbol"] for s in scan["skipped"] if s["reason"] == "not_owned"],
                         ["ETH_USDT"], "有仓但没我的痕迹 → 留痕、不扫")
        self.assertEqual([s for s in scan["skipped"] if s["symbol"] == "ETH_USDT"],
                         [{"symbol": "ETH_USDT", "reason": "not_owned"}])

    def test_single_coin_unchanged(self):
        """单币 + 无额外归属合约 → 逐字等于 `bot.symbols`（I11）。"""
        scan = guard_scan_set(_bot(["BTC_USDT"]), _StubClient())
        self.assertEqual(scan["symbols"], ["BTC_USDT"])
        self.assertEqual(scan["extra"], [])
        self.assertEqual(scan["skipped"], [])

    def test_over_cap_is_recorded(self):
        """币数超上限 → 截断 + `over_cap` 留痕（不能静默少扫）。"""
        cap = W._SCAN_SYMBOL_CAP
        syms = [f"C{i}_USDT" for i in range(cap + 2)]
        scan = guard_scan_set(_bot(syms), _StubClient())
        self.assertEqual(len(scan["symbols"]), cap)
        over = [s for s in scan["skipped"] if s["reason"] == "over_cap"]
        self.assertEqual(len(over), 2)

    def test_client_failure_is_recorded_not_raised(self):
        """取归属痕迹失败 → 不扩展、只记原因（不能让守护整轮崩）。"""
        class _Boom(_StubClient):
            def list_orders(self, contract=None):
                raise RuntimeError("network down")

        scan = guard_scan_set(_bot(["BTC_USDT"]), _Boom())
        self.assertEqual(scan["symbols"], ["BTC_USDT"])
        self.assertTrue(any("orders" in e for e in scan["errors"]), scan["errors"])

    def test_no_label_prefix_does_not_expand(self):
        """判不出本 bot 命名空间 → **不扩展**（fail-closed，不然会扫全账户）。"""
        scan = guard_scan_set(
            _bot(["BTC_USDT"], label_prefix=""),
            _StubClient(orders=[{"contract": "ETH_USDT", "text": "t-brk"}]),
        )
        self.assertEqual(scan["symbols"], ["BTC_USDT"])
        self.assertEqual(scan["extra"], [])


class TestToolUsageObservation(unittest.TestCase):
    def _runner(self, symbols) -> PlanRunner:
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=list(symbols), bot_id="b1")
        r.tool_usage = []
        return r

    def test_by_symbol_missing_and_out_of_universe(self):
        r = self._runner(["BTC_USDT", "ETH_USDT"])
        r._record_tool_use("klines", {"symbol": "BTC_USDT"}, "x",
                           raw_args={"symbol": "BTC_USDT"})
        r._record_tool_use("indicators", {}, "y", raw_args={})            # 漏写 → 记缺失
        r._record_tool_use("ticker", {"symbol": "SOL_USDT"}, "z",
                           raw_args={"symbol": "SOL_USDT"})               # 越界 → 记越界
        s = r._tool_usage_summary()
        self.assertEqual(s["counts"], {"klines": 1, "indicators": 1, "ticker": 1})
        self.assertEqual(s["total_calls"], 3)
        self.assertTrue(s["data_checked"])
        self.assertEqual(s["by_symbol"]["BTC_USDT"]["calls"], 1)
        self.assertEqual(s["by_symbol"]["BTC_USDT"]["tools"], ["klines"])
        self.assertEqual(s["missing_symbol"], 1)
        self.assertEqual(s["out_of_universe"], 1)
        self.assertNotIn("SOL_USDT", s["by_symbol"], "越界的币不该出现在按币归因里")

    def test_auto_filled_counts_as_missing(self):
        """单币宇宙下自动补全**也算** missing —— 币一多这些调用就会变成拒绝。"""
        r = self._runner(["BTC_USDT"])
        r._record_tool_use("klines", {}, "x", raw_args={})
        s = r._tool_usage_summary()
        self.assertEqual(s["missing_symbol"], 1)
        self.assertEqual(s["by_symbol"]["BTC_USDT"]["calls"], 1,
                         "自动补全后数据确实取自 BTC —— 按币归因要算它")

    def test_symbol_free_tools_not_counted(self):
        """与币无关的工具（skill / journal_lookup）不进 by_symbol、也不进 missing。"""
        r = self._runner(["BTC_USDT", "ETH_USDT"])
        r._record_tool_use("skill", {"name": "pa"}, "x", raw_args={})
        r._record_tool_use("journal_lookup", {"cycle_id": "c1"}, "y", raw_args={})
        s = r._tool_usage_summary()
        self.assertEqual(s["by_symbol"], {})
        self.assertEqual(s["missing_symbol"], 0)
        self.assertEqual(s["total_calls"], 2)

    def test_raw_args_are_the_source_of_truth(self):
        """`run_tool` 会就地回填 symbol —— 判「模型写没写」只能看原始参数。"""
        r = self._runner(["BTC_USDT", "ETH_USDT"])
        filled = {"symbol": "BTC_USDT"}          # run_tool 回填后的样子
        r._record_tool_use("klines", filled, "x", raw_args={})   # 模型其实没写
        self.assertEqual(r._tool_usage_summary()["missing_symbol"], 1)

    def test_old_fields_shape_unchanged(self):
        r = self._runner(["BTC_USDT"])
        r._record_tool_use("klines", {"symbol": "BTC_USDT"}, "x",
                           raw_args={"symbol": "BTC_USDT"})
        s = r._tool_usage_summary()
        self.assertEqual(set(s), {"counts", "total_calls", "data_checked",
                                 "by_symbol", "missing_symbol", "out_of_universe"})
        self.assertIsInstance(s["counts"], dict)
        self.assertIsInstance(s["data_checked"], bool)

    def test_run_once_resets_tool_usage(self):
        """每轮开始清零 —— 否则统计跨轮累加（旧实现只在 analyze_once 清）。"""
        r = self._runner(["BTC_USDT"])
        r.tool_usage = [{"tool": "stale-from-last-round"}]
        with mock.patch.object(PlanRunner, "_assemble_prompt",
                               side_effect=RuntimeError("boom")):
            try:
                r.run_once()
            except Exception:  # noqa: BLE001 — 只要走到清零那行就算
                pass
        self.assertEqual(r.tool_usage, [], "每轮开始必须清零")


class TestThinkingCharts(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def _runner(self) -> PlanRunner:
        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=["BTC_USDT"], bot_id="b1",
                                 bot_root=Path(self._td.name))
        r.history_dir = Path(self._td.name) / "state"
        r.inbox = Path(self._td.name) / "b1"
        r.tool_usage = []
        return r

    def test_charts_meta_persisted_without_base64(self):
        r = self._runner()
        r._last_chart_meta = [{"symbol": "BTC_USDT", "tf": "15m",
                               "file": "c1.png", "ok": True, "bytes": 12345}]
        r._save_thinking(cycle_id="c1", reasoning=["因为…"], content="{}")
        files = sorted(Path(r.history_dir).glob("*.thinking.json"))
        self.assertTrue(files, "thinking.json 没落盘")
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        self.assertEqual(payload["charts"][0]["symbol"], "BTC_USDT")
        self.assertEqual(payload["charts"][0]["tf"], "15m")
        blob = json.dumps(payload)
        self.assertNotIn("base64", blob, "只写元数据，不写图片本体")
        self.assertLess(len(blob), 65536, "charts 不该把文件撑大")

    def test_charts_empty_when_not_generated(self):
        """没生成图时是空数组（不是缺字段）—— 消费方不必做存在性判断。"""
        r = self._runner()
        r._save_thinking(cycle_id="c2")
        files = sorted(Path(r.history_dir).glob("*.thinking.json"))
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        self.assertEqual(payload["charts"], [])
        self.assertIn("tool_usage_summary", payload)


if __name__ == "__main__":
    unittest.main()
