# -*- coding: utf-8 -*-
"""aux 工具的符号形态匹配（`tech_analysis` 曾静默返回空）。

背景：`fetch_aux.py` 往 `tech_analysis_ts.symbol` 写的是**带下划线的合约名**
（`BTC_USDT`），而 `tech_analysis` 工具只试 `["BTCUSDT", "BTC"]` —— 三种形态里
少了一种，于是**查不到却静默返回空数组**（不报错，比报错更难发现）。

本测试用夹具库把三种形态各放一行，断言都能命中。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.strategist.tools import run_tool

SCHEMA = """
CREATE TABLE tech_analysis_ts (
  fetched_ts REAL, symbol TEXT, period TEXT, signal TEXT, timeframes_json TEXT
);
"""


def _make_db(path: Path, symbols) -> None:
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    for i, s in enumerate(symbols):
        c.execute(
            "INSERT INTO tech_analysis_ts VALUES (?,?,?,?,?)",
            (1000.0 + i, s, "15m", "buy", "[]"),
        )
    c.commit()
    c.close()


def _call(symbol: str) -> dict:
    return run_tool(None, "tech_analysis", {"symbol": symbol}, env="live",
                    bot_root=None, market_cfg=None, bot_id="t")


class TestAuxSymbolForms(unittest.TestCase):
    def _with_db(self, symbols):
        td = tempfile.TemporaryDirectory()
        db = Path(td.name) / "aux_cache.db"
        _make_db(db, symbols)
        return td, db

    def test_underscore_form_is_found(self):
        """fetch_aux 实际写的 `BTC_USDT` 形态必须命中 —— 这是原先漏掉的那种。"""
        td, db = self._with_db(["BTC_USDT"])
        try:
            with mock.patch.dict(os.environ, {"OMNIALPHA_AUX": str(db)}):
                out = _call("BTC_USDT")
        finally:
            td.cleanup()
        self.assertTrue(out.get("tech_analysis"), f"应为非空，实际 {out}")

    def test_compact_form_still_works(self):
        td, db = self._with_db(["BTCUSDT"])
        try:
            with mock.patch.dict(os.environ, {"OMNIALPHA_AUX": str(db)}):
                out = _call("BTC_USDT")
        finally:
            td.cleanup()
        self.assertTrue(out.get("tech_analysis"), f"应为非空，实际 {out}")

    def test_bare_coin_form_still_works(self):
        td, db = self._with_db(["BTC"])
        try:
            with mock.patch.dict(os.environ, {"OMNIALPHA_AUX": str(db)}):
                out = _call("BTC_USDT")
        finally:
            td.cleanup()
        self.assertTrue(out.get("tech_analysis"), f"应为非空，实际 {out}")

    def test_missing_symbol_returns_empty_not_error(self):
        """真的没有这个符号 → 空数组（而不是 error），保持原语义。"""
        td, db = self._with_db(["ETH_USDT"])
        try:
            with mock.patch.dict(os.environ, {"OMNIALPHA_AUX": str(db)}):
                out = _call("BTC_USDT")
        finally:
            td.cleanup()
        self.assertEqual(out.get("tech_analysis"), [])


if __name__ == "__main__":
    unittest.main()
