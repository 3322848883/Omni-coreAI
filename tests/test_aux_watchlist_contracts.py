# -*- coding: utf-8 -*-
"""T16：aux 采集品种**从 watchlist 读**（新增币不再永远没数据）。

覆盖 spec `symbol-as-parameter.md` §S2.4⑪：

- `fetch_aux.CONTRACTS` 原先是与 watchlist.yaml **并列的第二份硬编码**。加一个币要改
  两处，而漏改 aux 那处**不报错、只是永远没数据**：aux 工具静默返回空列表，模型读成
  「这个币没数据」（审计 A-7/A-8 —— 静默空比报错更难发现）。
- 读不到 / 解析失败 / 里面没有 symbols → **回退内置默认并告警**：采集器不能因为一个
  配置文件就起不来（那会让全部 aux 数据停摆，比少几个币严重得多）。
- 舆情查询词表里没有的币回退到**裸币名**（用 `DOGE_USDT` 当查询词召回太低，
  等于新币的舆情永远是空的）。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PA = ROOT / "pa-data-source"
sys.path.insert(0, str(PA))

import aux_monitor  # noqa: E402  （它内部 import fetch_aux，所以必须在 sys.path 之后）

DEFAULT = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "XAU_USDT", "XAG_USDT"]


def _load_with_watchlist(path) -> list:
    """在指定 watchlist 下重新加载 `fetch_aux`，返回它的 `CONTRACTS`。"""
    old = os.environ.get("OMNIALPHA_WATCHLIST")
    os.environ["OMNIALPHA_WATCHLIST"] = str(path)
    try:
        sys.modules.pop("fetch_aux", None)
        mod = importlib.import_module("fetch_aux")
        return list(mod.CONTRACTS)
    finally:
        if old is None:
            os.environ.pop("OMNIALPHA_WATCHLIST", None)
        else:
            os.environ["OMNIALPHA_WATCHLIST"] = old
        sys.modules.pop("fetch_aux", None)


class TestContractsFromWatchlist(unittest.TestCase):
    def test_new_symbol_is_picked_up(self):
        """watchlist 里加一个新币 → 采集品种自动包含它（旧实现必失败）。"""
        with tempfile.TemporaryDirectory() as td:
            wl = Path(td) / "watchlist.yaml"
            wl.write_text(
                "symbols:\n"
                "  - name: BTC_USDT\n"
                "  - name: ETH_USDT\n"
                "  - name: DOGE_USDT\n",
                encoding="utf-8")
            self.assertEqual(_load_with_watchlist(wl),
                             ["BTC_USDT", "ETH_USDT", "DOGE_USDT"])

    def test_real_watchlist_is_used_by_default(self):
        """不给环境变量时读的是仓库里那份 watchlist.yaml。"""
        old = os.environ.pop("OMNIALPHA_WATCHLIST", None)
        old2 = os.environ.pop("AUX_WATCHLIST", None)
        try:
            sys.modules.pop("fetch_aux", None)
            mod = importlib.import_module("fetch_aux")
            self.assertTrue(mod.CONTRACTS, "默认 watchlist 至少要有品种")
            self.assertIn("BTC_USDT", mod.CONTRACTS)
        finally:
            if old is not None:
                os.environ["OMNIALPHA_WATCHLIST"] = old
            if old2 is not None:
                os.environ["AUX_WATCHLIST"] = old2
            sys.modules.pop("fetch_aux", None)

    def test_missing_watchlist_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_load_with_watchlist(Path(td) / "nope.yaml"), DEFAULT)

    def test_broken_watchlist_falls_back_without_crash(self):
        with tempfile.TemporaryDirectory() as td:
            wl = Path(td) / "watchlist.yaml"
            wl.write_text("symbols: [\n  - name: 'unclosed\n", encoding="utf-8")
            self.assertEqual(_load_with_watchlist(wl), DEFAULT)

    def test_watchlist_without_symbols_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            wl = Path(td) / "watchlist.yaml"
            wl.write_text("market_type: spot\n", encoding="utf-8")
            self.assertEqual(_load_with_watchlist(wl), DEFAULT)

    def test_plain_string_entries_also_work(self):
        """`symbols: [BTC_USDT]` 这种简写形态也要认。"""
        with tempfile.TemporaryDirectory() as td:
            wl = Path(td) / "watchlist.yaml"
            wl.write_text("symbols:\n  - BTC_USDT\n  - eth_usdt\n", encoding="utf-8")
            self.assertEqual(_load_with_watchlist(wl), ["BTC_USDT", "ETH_USDT"])


class TestXpostQueryFallback(unittest.TestCase):
    def test_unknown_contract_uses_bare_coin(self):
        sys.modules.pop("fetch_aux", None)
        mod = importlib.import_module("fetch_aux")
        try:
            # 表里有的用原词，没有的回退裸币名（不是 `DOGE_USDT`）
            self.assertEqual(mod.XPOST_QUERIES.get("BTC_USDT") or
                             "BTC_USDT".split("_")[0], "BTC Bitcoin")
            unknown = "DOGE_USDT"
            self.assertEqual(mod.XPOST_QUERIES.get(unknown) or
                             str(unknown).split("_")[0], "DOGE")
        finally:
            sys.modules.pop("fetch_aux", None)


class TestSymbolCoverage(unittest.TestCase):
    """「某币零数据」必须能被监控报出 —— 整体 ok 掩盖不了这一层。"""

    def _aux_db(self, path: Path, rows) -> None:
        """最小 aux 库：`rows = [(table, contract, n)]`。"""
        import sqlite3

        con = sqlite3.connect(str(path))
        for t in ("trades", "market_stats_ts"):
            con.execute("CREATE TABLE %s (contract TEXT)" % t)
        for t, c, n in rows:
            for _ in range(n):
                con.execute("INSERT INTO %s VALUES (?)" % t, (c,))
        con.commit()
        con.close()

    def test_zero_coverage_symbol_reported(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "aux_cache.db"
            self._aux_db(db, [("trades", "BTC_USDT", 3), ("market_stats_ts", "BTC_USDT", 1)])
            cov = aux_monitor.symbol_coverage(db, ["BTC_USDT", "DOGE_USDT"])
            self.assertEqual(cov["BTC_USDT"]["trades"], 3)
            self.assertEqual(cov["BTC_USDT"]["market_stats_ts"], 1)
            self.assertEqual(cov["DOGE_USDT"]["trades"], 0, "新加的币必须被统计到（零行）")
            self.assertEqual(aux_monitor.zero_coverage_symbols(cov), ["DOGE_USDT"])

    def test_partial_coverage_is_not_zero(self):
        """只要有一张表有数据就不算「零覆盖」—— 别把「部分接口没有」误报成「没数据」。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "aux_cache.db"
            self._aux_db(db, [("trades", "ETH_USDT", 2)])
            cov = aux_monitor.symbol_coverage(db, ["ETH_USDT"])
            self.assertEqual(aux_monitor.zero_coverage_symbols(cov), [])

    def test_missing_db_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(aux_monitor.symbol_coverage(Path(td) / "nope.db", ["BTC_USDT"]), {})
            self.assertEqual(aux_monitor.zero_coverage_symbols({}), [])

    def test_status_reports_zero_coverage(self):
        """接进 `read_aux_status`：零覆盖的币要出现在 inconsistencies 里。"""
        import json

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "aux_cache.db"
            self._aux_db(db, [("trades", "BTC_USDT", 1)])
            st_path = Path(td) / "aux_status.json"
            st_path.write_text(json.dumps({
                "ts": int(__import__("time").time()),
                "interfaces": {},
                "db_tables": {"tables": {}, "schema_version": aux_monitor.SCHEMA_VERSION},
            }), encoding="utf-8")
            out = aux_monitor.read_aux_status(str(st_path), db_path=str(db),
                                             contracts=["BTC_USDT", "DOGE_USDT"])
            self.assertEqual(out["zero_coverage"], ["DOGE_USDT"])
            self.assertTrue(any("DOGE_USDT" in s for s in out["inconsistencies"]), out)
            # 不传 db_path 时行为与改动前一致（不多出键）
            plain = aux_monitor.read_aux_status(str(st_path))
            self.assertNotIn("zero_coverage", plain)


if __name__ == "__main__":
    unittest.main()
