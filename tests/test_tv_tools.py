"""TV 指标工具测试（tv_linreg_trendlines / tv_rsi_yata / tv_lr_ha_candles）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.tools import NATIVE_TOOLS, TOOL_NAMES  # noqa: E402
from omnialpha.strategist.tv_tools import TV_TOOL_DEFS, TV_TOOL_NAMES, run_tv_tool  # noqa: E402


def _fake_rows(n=120, base=100.0):
    """造一段确定性 OHLC（正弦+趋势），供工具计算。"""
    import math

    rows = []
    for i in range(n):
        c = base + math.sin(i / 7) * 3 + i * 0.05
        o = c - 0.3
        h = max(o, c) + 0.8
        l = min(o, c) - 0.8
        rows.append({"t": i, "o": o, "h": h, "l": l, "c": c, "v": 10.0 + i})
    return rows


class _FakeRes:
    def __init__(self, rows):
        self.rows = rows
        self.source = "test"


class TestTvToolRegistration(unittest.TestCase):
    def test_eight_tools_registered(self):
        names = [t["function"]["name"] for t in TV_TOOL_DEFS]
        self.assertEqual(names, [
            "tv_linreg_trendlines", "tv_rsi_yata", "tv_lr_ha_candles",
            "tv_delta_flow_profile", "tv_oi_visible_range", "tv_vol_oi_footprint",
            "tv_cdv", "tv_wyckoff",
        ])

    def test_names_match_real_indicators(self):
        """工具名对应 TV 原始指标名（非缩写）。"""
        self.assertIn("tv_linreg_trendlines", TV_TOOL_NAMES)  # Linreg & Trendlines
        self.assertIn("tv_rsi_yata", TV_TOOL_NAMES)           # RSI Yata
        self.assertIn("tv_lr_ha_candles", TV_TOOL_NAMES)      # LR HA Candles
        self.assertIn("tv_wyckoff", TV_TOOL_NAMES)            # Wyckoff [theUltimator5]

    def test_in_native_tools(self):
        names = [t["function"]["name"] for t in NATIVE_TOOLS]
        for n in TV_TOOL_NAMES:
            self.assertIn(n, names)
            self.assertIn(n, TOOL_NAMES)

    def test_tool_count(self):
        # 27 基础（含 taker_delta + 4 个 orderflow）+ 8 TV
        self.assertEqual(len(NATIVE_TOOLS), 35)

    def test_tool_names_exactly_match_native_tools(self):
        """`TOOL_NAMES` 是手写清单，必须与 `NATIVE_TOOLS` 完全一致。

        不一致时 `run_tool` 会把真实存在的工具判成 `unknown tool`——
        模型能看到工具（工具面来自 NATIVE_TOOLS），一调却被拒。加新工具时最容易漏这里。
        """
        declared = [t["function"]["name"] for t in NATIVE_TOOLS]
        self.assertEqual(sorted(TOOL_NAMES), sorted(declared))
        self.assertEqual(len(TOOL_NAMES), len(set(TOOL_NAMES)), "TOOL_NAMES 有重复项")


class TestTvToolRun(unittest.TestCase):
    def _run(self, name, args):
        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(_fake_rows())):
            return run_tv_tool(None, name, args, env="testnet", bot_root=ROOT)

    def test_linreg_trendlines_output(self):
        r = self._run("tv_linreg_trendlines", {"symbol": "BTC_USDT", "tf": "1h", "length": 50})
        self.assertEqual(r["symbol"], "BTC_USDT")
        self.assertEqual(r["bars"], 120)
        ch = r["channel"]
        self.assertIn("base", ch)
        self.assertEqual(len(ch["layers"]), 3)
        self.assertEqual([l["mult"] for l in ch["layers"]], [1.0, 2.0, 3.0])
        self.assertIsNotNone(ch["slope"])
        self.assertIsNotNone(ch["std_dev"])
        self.assertIn("primary", r["trendlines"])

    def test_rsi_yata_output(self):
        r = self._run("tv_rsi_yata", {"symbol": "BTC_USDT", "tf": "1h", "length": 14})
        self.assertIsNotNone(r["rsi_last"])
        self.assertGreaterEqual(r["rsi_last"], 0.0)
        self.assertLessEqual(r["rsi_last"], 100.0)
        self.assertEqual(len(r["rsi_series_tail"]), 5)
        for k in ("ma", "upper", "lower"):
            self.assertIsNotNone(r["bollinger_last"][k])
        for k in ("open", "high", "low", "close"):
            self.assertIsNotNone(r["rsi_candles_last"][k])
        self.assertIsInstance(r["ob_os_recent"], list)
        self.assertIsInstance(r["structure_labels"], list)

    def test_lr_ha_candles_output(self):
        r = self._run("tv_lr_ha_candles", {"symbol": "BTC_USDT", "tf": "1h", "length": 9})
        for k in ("open", "high", "low", "close"):
            self.assertIsNotNone(r["heikin_ashi_last"][k])
            self.assertIsNotNone(r["lr_ha_last"][k])
        self.assertIsNotNone(r["t3_last"])
        vb = r["volatility_bands_last"]
        for k in ("basis", "upper_inner", "lower_inner", "upper_outer", "lower_outer"):
            self.assertIsNotNone(vb[k])
        # 上轨 > basis > 下轨
        self.assertGreater(vb["upper_inner"], vb["basis"])
        self.assertLess(vb["lower_inner"], vb["basis"])

    def test_insufficient_candles(self):
        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(_fake_rows(5))):
            r = run_tv_tool(None, "tv_rsi_yata", {"symbol": "X"}, env="testnet", bot_root=ROOT)
        self.assertIn("error", r)

    def test_unknown_tv_tool(self):
        r = run_tv_tool(None, "tv_nope", {})
        self.assertIn("error", r)


class TestTvToolViaRunTool(unittest.TestCase):
    """通过 run_tool 分派（含 tv_ 前缀路由）。"""

    def test_dispatch(self):
        from omnialpha.strategist import tools as T

        with mock.patch("omnialpha.strategist.tv_tools.resolve_candles",
                        return_value=_FakeRes(_fake_rows())):
            r = T.run_tool(None, "tv_lr_ha_candles",
                           {"symbol": "BTC_USDT", "tf": "1h"}, env="testnet", bot_root=ROOT)
        self.assertIn("lr_ha_last", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
