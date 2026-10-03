# -*- coding: utf-8 -*-
"""工具面按数据源可用性过滤（`available_native_tools`）。

背景：`_aux_query()` 依赖 pa-data-source 的 `aux_cache.db`；该文件不在时，那 10 个
aux 工具**每次调用只会返回 `aux_cache.db not found`**。实盘实测：trades_flow 19/19、
market_stats 1/1、liquidations 1/1 全失败 —— 占累计 286 次调用的 7.3%，模型白烧轮次。

修法：数据源不在时**不把这些工具挂给模型**（模型看不到就调不到）。

注：`_aux_db()` 在未设环境变量时还会回退查 `Path.cwd()`，本机恰好有那个文件，
所以「缺失」用例一律用 `OMNIALPHA_AUX` 指向不存在的路径来短路，保证确定性。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.strategist.tools import (
    AUX_TOOL_NAMES,
    NATIVE_TOOLS,
    available_native_tools,
)

CORE = ("klines", "indicators", "ticker", "orderbook", "account",
        "smc_map", "smc_events", "sqzmom", "skill", "skill_ref",
        "tv_linreg_trendlines", "tv_rsi_yata", "tv_lr_ha_candles")


def _names(tools) -> set:
    return {(t.get("function") or {}).get("name") for t in tools}


class TestAvailableNativeTools(unittest.TestCase):
    def test_aux_names_all_exist(self):
        """AUX_TOOL_NAMES 必须与 NATIVE_TOOLS 的真实名字一致 —— 防写错、防改名漂移。"""
        self.assertTrue(set(AUX_TOOL_NAMES) <= _names(NATIVE_TOOLS),
                        f"这些名字不在 NATIVE_TOOLS 里: {set(AUX_TOOL_NAMES) - _names(NATIVE_TOOLS)}")

    def test_aux_dropped_when_data_source_missing(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "nope.db")
            with mock.patch.dict(os.environ, {"OMNIALPHA_AUX": missing}):
                names = _names(available_native_tools(td))
        for n in AUX_TOOL_NAMES:
            self.assertNotIn(n, names, f"{n} 应当被摘掉")
        for n in CORE:
            self.assertIn(n, names, f"{n} 必须保留")

    def test_all_kept_when_data_source_present(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "aux_cache.db"
            db.write_bytes(b"")
            with mock.patch.dict(os.environ, {"OMNIALPHA_AUX": str(db)}):
                names = _names(available_native_tools(td))
        self.assertTrue(set(AUX_TOOL_NAMES) <= names)
        self.assertEqual(names, _names(NATIVE_TOOLS))

    def test_bot_root_lookup_finds_db(self):
        """未设环境变量时按 <bot_root>/pa-data-source/aux-data/aux_cache.db 找。

        cwd 切到空目录，排除 cwd 回退的干扰。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "botroot"
            d = root / "pa-data-source" / "aux-data"
            d.mkdir(parents=True)
            (d / "aux_cache.db").write_bytes(b"")
            empty = Path(td) / "empty"
            empty.mkdir()
            old = os.getcwd()
            os.chdir(empty)
            try:
                env = {k: v for k, v in os.environ.items()
                       if k not in ("OMNIALPHA_AUX", "GATE_AUX_DB")}
                with mock.patch.dict(os.environ, env, clear=True):
                    names = _names(available_native_tools(root))
            finally:
                os.chdir(old)
        self.assertIn("trades_flow", names)

    def test_bot_root_lookup_missing_drops(self):
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "empty"
            empty.mkdir()
            old = os.getcwd()
            os.chdir(empty)
            try:
                env = {k: v for k, v in os.environ.items()
                       if k not in ("OMNIALPHA_AUX", "GATE_AUX_DB")}
                with mock.patch.dict(os.environ, env, clear=True):
                    names = _names(available_native_tools(Path(td) / "botroot"))
            finally:
                os.chdir(old)
        self.assertNotIn("trades_flow", names)
        self.assertIn("klines", names)


if __name__ == "__main__":
    unittest.main()
