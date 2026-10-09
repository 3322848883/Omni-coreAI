# -*- coding: utf-8 -*-
"""aux 工具的 symbol：**形态归一 + 缺 symbol 拒绝**。

回归背景（设计文档 A-7 / A-8）：

- `fetch_aux.py` 往 aux_cache 写的是**带下划线的合约名**（`BTC_USDT`），而
  `trades_flow`/`liquidations`/`market_stats`/`coin_info`/`onchain`/`social`
  只按**一种**形态查 —— 查不到就返回 `[]`，模型读成「这个币没数据」。
  **静默空比报错更难发现**（只有 `tech_analysis` 当时试了三种形态）。
- `sentiment` 更危险：缺 coin 时走「全表最新 N 行」，返回**任意币**的情绪，
  返回体里连币名都没有 → 模型的整个判断建立在错误标的上，且**零告警**。

所以这里钉住两件事：三种形态都要能命中；缺 symbol 必须**拒绝**而不是查全表。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.tools import run_tool  # noqa: E402

MULTI = ["BTC_USDT", "ETH_USDT"]

# aux_cache 里各表被工具读到的列（列名必须与工具 SQL 一致，否则测的是别的东西）
SCHEMA = """
CREATE TABLE trades (time REAL, contract TEXT, price REAL, size REAL);
CREATE TABLE liquidations (
  time REAL, contract TEXT, size REAL, order_size REAL,
  order_price REAL, fill_price REAL
);
CREATE TABLE market_stats_ts (
  fetched_ts REAL, contract TEXT, lsr_taker REAL, lsr_account REAL,
  long_liq_size REAL, short_liq_size REAL, open_interest REAL,
  open_interest_usd REAL, top_lsr_account REAL, mark_price REAL
);
CREATE TABLE coin_info_ts (
  fetched_ts REAL, symbol TEXT, name TEXT, chain TEXT, category TEXT,
  market_value REAL, sentiment_score REAL
);
CREATE TABLE onchain_ts (
  fetched_ts REAL, token TEXT, chain TEXT, daily_active_addresses REAL,
  daily_transfer_volume REAL, new_address_count_7d REAL, holder_count REAL,
  data_quality TEXT
);
CREATE TABLE social_posts_ts (
  fetched_ts REAL, coin TEXT, author TEXT, content TEXT, upvotes REAL,
  sentiment_label TEXT, sentiment_score REAL
);
CREATE TABLE sentiment_ts (
  fetched_ts REAL, coin TEXT, mention_count REAL, overall_sentiment REAL,
  sentiment_label TEXT, positive_ratio REAL, neutral_ratio REAL,
  negative_ratio REAL
);
"""

# 工具名 → (表名, 该表用来对币的列, 结果字段名, 返回体里的币字段名)
PER_COIN = {
    "trades_flow": ("trades", "contract", "trades", "symbol"),
    "liquidations": ("liquidations", "contract", "liquidations", "symbol"),
    "market_stats": ("market_stats_ts", "contract", "market_stats", "symbol"),
    "coin_info": ("coin_info_ts", "symbol", "coin_info", "symbol"),
    "onchain": ("onchain_ts", "token", "onchain", "token"),
    "social": ("social_posts_ts", "coin", "social", "coin"),
    "sentiment": ("sentiment_ts", "coin", "sentiment", "symbol"),
}

FORMS = ("BTC_USDT", "BTCUSDT", "BTC")


def _make_db(path: Path, key: str) -> None:
    """把每个 aux 表都塞一行，币名用 `key`（三种形态之一）。"""
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    c.execute("INSERT INTO trades VALUES (?,?,?,?)", (1.0, key, 100.0, 1.0))
    c.execute("INSERT INTO liquidations VALUES (?,?,?,?,?,?)",
              (1.0, key, -1.0, 1.0, 100.0, 100.0))
    c.execute("INSERT INTO market_stats_ts VALUES (?,?,?,?,?,?,?,?,?,?)",
              (1.0, key, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 100.0))
    c.execute("INSERT INTO coin_info_ts VALUES (?,?,?,?,?,?,?)",
              (1.0, key, "Bitcoin", "bitcoin", "pow", 1.0, 50.0))
    c.execute("INSERT INTO onchain_ts VALUES (?,?,?,?,?,?,?,?)",
              (1.0, key, "bitcoin", 1.0, 1.0, 1.0, 1.0, "ok"))
    c.execute("INSERT INTO social_posts_ts VALUES (?,?,?,?,?,?,?)",
              (1.0, key, "a", "hello", 1.0, "positive", 0.5))
    c.execute("INSERT INTO sentiment_ts VALUES (?,?,?,?,?,?,?,?)",
              (1.0, key, 1.0, 0.5, "positive", 0.5, 0.3, 0.2))
    c.commit()
    c.close()


class _AuxDbCase(unittest.TestCase):
    def _call(self, tool: str, args: dict, key: str, universe=None) -> dict:
        td = tempfile.TemporaryDirectory()
        db = Path(td.name) / "aux_cache.db"
        _make_db(db, key)
        try:
            with mock.patch.dict(os.environ, {"OMNIALPHA_AUX": str(db)}):
                return run_tool(None, tool, dict(args), env="live", bot_root=None,
                                market_cfg=None, bot_id="t", symbols=universe)
        finally:
            td.cleanup()


class TestAuxFormsAreNormalised(_AuxDbCase):
    """三种形态都要命中 —— 少试一种就是「查不到却静默返回空」。"""

    def test_all_three_forms_found(self):
        for tool, (_tbl, _col, field, _key) in PER_COIN.items():
            for form in FORMS:
                with self.subTest(tool=tool, stored_as=form):
                    out = self._call(tool, {"symbol": "BTC_USDT"}, form, MULTI)
                    self.assertTrue(out.get(field),
                                    f"{tool}: 库里存 {form} 时查不到（返回 {out}）")

    def test_other_coin_is_not_returned(self):
        """查不到就是空 —— 但**不得**因此退回别的币（不静默跨币）。"""
        for tool, (_tbl, _col, field, _key) in PER_COIN.items():
            with self.subTest(tool=tool):
                out = self._call(tool, {"symbol": "BTC_USDT"}, "ETH_USDT", MULTI)
                self.assertEqual(out.get(field), [],
                                 f"{tool} 查不到时返回了别人的数据：{out}")


class TestAuxRequiresSymbol(_AuxDbCase):
    """多币宇宙下缺 symbol → 拒绝（附宇宙），绝不查全表。"""

    def test_per_coin_tools_reject_missing_symbol(self):
        for tool in PER_COIN:
            with self.subTest(tool=tool):
                out = run_tool(None, tool, {}, symbols=MULTI)
                self.assertEqual(out.get("error"), "symbol_required", (tool, out))
                self.assertEqual(out.get("universe"), MULTI, tool)

    def test_sentiment_missing_coin_does_not_scan_all_coins(self):
        """`sentiment` 缺 coin 曾经查全表 → 返回任意币的情绪（A-8，安全级）。"""
        out = self._call("sentiment", {}, "ETH_USDT", MULTI)
        self.assertEqual(out.get("error"), "symbol_required", out)
        self.assertNotIn("sentiment", out, "拒绝时不得再带上任何币的情绪数据")

    def test_coin_alias_still_accepted(self):
        """schema 里 aux 用的是 `coin`/`token`，别名不该被误判成「缺 symbol」。"""
        out = self._call("sentiment", {"coin": "BTC_USDT"}, "BTC_USDT", MULTI)
        self.assertTrue(out.get("sentiment"), out)

    def test_single_universe_autofills(self):
        out = self._call("sentiment", {}, "BTC_USDT", ["BTC_USDT"])
        self.assertTrue(out.get("sentiment"), out)


class TestGlobalAuxToolsAreNotForced(unittest.TestCase):
    """`overview` / `macro` 与币无关 —— 强制要 symbol 只会误伤。"""

    def test_global_tools_not_gated(self):
        for tool in ("overview", "macro"):
            with self.subTest(tool=tool):
                out = run_tool(None, tool, {}, symbols=MULTI)
                self.assertNotEqual(out.get("error"), "symbol_required", (tool, out))

    def test_global_tools_absent_from_symbol_set(self):
        from omnialpha.strategist.tools import SYMBOL_TOOLS
        for tool in ("overview", "macro", "account", "skill", "skill_ref", "journal_lookup"):
            self.assertNotIn(tool, SYMBOL_TOOLS, f"{tool} 不该被强制要 symbol")


if __name__ == "__main__":
    unittest.main(verbosity=2)
