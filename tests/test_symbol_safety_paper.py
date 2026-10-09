# -*- coding: utf-8 -*-
"""T19（第一批）：安全批 —— HL 撤单不猜币 / paper 平一侧不裸另一侧 / 逐合约杠杆 / 按币快照。

覆盖 spec `symbol-as-parameter.md` §S2.3 #1/#6/#7：

- `hyperliquid.cancel_order` 原先硬编码 `coin: "BTC"` —— 对任何非 BTC 的撤单要么打到
  BTC 的单上、要么以「coin 不匹配」失败。现在 coin 只从「这个 oid 属于哪个合约」推，
  **推不出来就拒绝**。
- paper 双向模式下同一 contract 的 long/short 各有保护单；平掉一侧时**只能撤那一侧**的，
  否则另一侧裸奔。
- paper 的杠杆原先写账户级一份，多币下后设的覆盖先设的 → 改为**逐合约**。
- `pnl_snapshot` 补 `contract` 列（原先是只有账户级合并值）：多币下「这个币赚没赚」
  用合并值答不出来。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.exchanges.base import ExchangeError  # noqa: E402
from omnialpha.exchanges.hyperliquid import HyperliquidExchange  # noqa: E402
from omnialpha.paper.exchange import PaperExchange  # noqa: E402
from omnialpha.paper.store import PaperStore  # noqa: E402
from tests.test_paper import FakeFeed, paper_env  # noqa: E402


class TestHyperliquidCancelCoin(unittest.TestCase):
    """撤单必须带**正确的** coin —— 硬编码 BTC 是实盘安全级缺陷。"""

    def _ex(self):
        ex = HyperliquidExchange(env="live", api_key="k", api_secret="s")
        self.posted: list = []

        def _fake_post(type_, payload, signed=False):
            self.posted.append((type_, payload))
            return {"ok": True}

        ex._post = _fake_post  # type: ignore[method-assign]
        return ex

    def test_coin_from_explicit_contract(self):
        ex = self._ex()
        ex.cancel_order("oid-1", contract="ETH_USDT")
        self.assertEqual(self.posted[-1][1]["cancels"], [{"coin": "ETH", "oid": "oid-1"}],
                         "非 BTC 撤单必须带自己的 coin")

    def test_coin_looked_up_from_open_orders(self):
        ex = self._ex()
        with mock.patch.object(ex, "list_orders", return_value=[
            {"id": "oid-9", "contract": "SOL_USDT"},
        ]):
            ex.cancel_order("oid-9")
        self.assertEqual(self.posted[-1][1]["cancels"], [{"coin": "SOL", "oid": "oid-9"}])

    def test_unknown_order_is_refused_not_guessed(self):
        """反查不到就**拒绝** —— 宁可报错，也不要撤错币（原先会撤 BTC）。"""
        ex = self._ex()
        with mock.patch.object(ex, "list_orders", return_value=[]):
            with self.assertRaises(ExchangeError):
                ex.cancel_order("oid-unknown")
        self.assertEqual(self.posted, [], "拒绝时不得发出任何撤单请求")


class TestPaperPerSideProtections(unittest.TestCase):
    """双向模式下平一侧，不能把另一侧的保护单一起撤掉。"""

    def _po_status(self, store, symbol):
        return [str(p["status"]) for p in store.list_price_orders(symbol)]

    def test_close_one_side_keeps_other_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, _feed, eng):
                store.upsert_position("BTC_USDT", "long", size=20.0, entry_price=100.0,
                                      leverage=20, margin=100.0, margin_mode="isolated")
                store.upsert_position("BTC_USDT", "short", size=-5.0, entry_price=110.0,
                                      leverage=20, margin=27.5, margin_mode="isolated")
                # 平多侧保护单（卖，size 负）与平空侧保护单（买，size 正）
                eng.place_price_order({
                    "initial": {"contract": "BTC_USDT", "size": -20, "price": "0",
                                "tif": "ioc", "text": "t-long-sl", "reduce_only": True},
                    "trigger": {"rule": 2, "price_type": 0, "price": "90"},
                })
                eng.place_price_order({
                    "initial": {"contract": "BTC_USDT", "size": 5, "price": "0",
                                "tif": "ioc", "text": "t-short-sl", "reduce_only": True},
                    "trigger": {"rule": 1, "price_type": 0, "price": "120"},
                })
                store.cancel_reduce_only_price_orders("BTC_USDT", mode="long")
                alive = [p for p in store.list_price_orders("BTC_USDT")
                         if str(p["status"]) in ("untriggered", "open", "triggered")]
                self.assertEqual([p["text"] for p in alive], ["t-short-sl"],
                                 "平多侧时空侧的保护单必须原样保留")
                self.assertTrue(any("orphan" in str(p.get("error") or "")
                                    for p in store.list_price_orders("BTC_USDT")))

    def test_single_mode_clears_all(self):
        """单仓模式（mode='single'）保持原行为：撤该合约全部未触发保护单。"""
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, _feed, eng):
                for text, size in (("t-a-sl", -20), ("t-b-tp", 5)):
                    eng.place_price_order({
                        "initial": {"contract": "BTC_USDT", "size": size, "price": "0",
                                    "tif": "ioc", "text": text, "reduce_only": True},
                        "trigger": {"rule": 2, "price_type": 0, "price": "90"},
                    })
                store.cancel_reduce_only_price_orders("BTC_USDT", mode="single")
                alive = [p for p in store.list_price_orders("BTC_USDT")
                         if str(p["status"]) in ("untriggered", "open", "triggered")]
                self.assertEqual(alive, [])


class TestPaperPerContractLeverage(unittest.TestCase):
    """杠杆按合约存 —— 多币下两个币要不同杠杆必须能表达。"""

    def test_exchange_set_leverage_is_per_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "account.db"
            seed = PaperStore(db)
            seed.init_config({"initial_capital": "10000", "leverage": "20"})
            seed.close()                      # 先关：Windows 下同文件双开会让 cleanup 失败
            ex = PaperExchange(env="paper", store_path=db, feed=FakeFeed())
            try:
                ex.set_leverage("BTC_USDT", 7)
                ex.set_leverage("ETH_USDT", 25)
                self.assertEqual(ex.store.cfg_f("leverage:BTC_USDT", 0), 7.0)
                self.assertEqual(ex.store.cfg_f("leverage:ETH_USDT", 0), 25.0,
                                 "后设的不许覆盖先设的（账户级一份就会覆盖）")
                self.assertEqual(float(ex.store.get_account().get("leverage") or 0), 20.0,
                                 "账户级默认杠杆不被改写")
            finally:
                try:
                    ex.store.close()
                except Exception:  # noqa: BLE001
                    pass

    def test_two_symbols_read_back_their_own_leverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, _feed, _eng):
                store.set_config("leverage:BTC_USDT", 10)   # ← exchange.set_leverage 写的就是这个键
                store.set_config("leverage:ETH_USDT", 50)
                self.assertEqual(store.cfg_f("leverage:BTC_USDT", 0), 10.0)
                self.assertEqual(store.cfg_f("leverage:ETH_USDT", 0), 50.0)

    def test_place_order_uses_that_contracts_leverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, _feed, eng):
                store.set_config("leverage:ETH_USDT", 33)
                seen: list = []

                def _capture(body, meta, last, **kw):
                    seen.append(kw.get("leverage"))
                    raise RuntimeError("stop here")

                with mock.patch("omnialpha.paper.engine.validate_and_round_order",
                                side_effect=_capture):
                    try:
                        eng.place_order({"contract": "ETH_USDT", "size": 1, "type": "market"})
                    except Exception:  # noqa: BLE001
                        pass
                self.assertEqual(seen and seen[0], 33.0,
                                 "下单要用**该合约**设的杠杆，不是账户级那一份")


class TestPnlSnapshotPerContract(unittest.TestCase):
    def test_snapshots_are_per_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            with paper_env(tmp) as (store, _feed, _eng):
                store.upsert_position("BTC_USDT", "single", size=1.0, entry_price=100.0,
                                      leverage=20, margin=5.0, margin_mode="isolated",
                                      realised_pnl=7.0)
                store.upsert_position("ETH_USDT", "single", size=1.0, entry_price=50.0,
                                      leverage=20, margin=2.5, margin_mode="isolated",
                                      realised_pnl=-3.0)
                eng_snap = {"snap_time": 1, "equity": 100.0, "unrealised": 0.0,
                            "realised": 4.0, "drawdown": 0.0}
                store.insert_pnl(eng_snap)
                store.insert_pnl({**eng_snap, "realised": 7.0, "contract": "BTC_USDT"})
                store.insert_pnl({**eng_snap, "realised": -3.0, "contract": "ETH_USDT"})
                by_c = store.pnl_by_contract()
                self.assertEqual(sorted(by_c), ["BTC_USDT", "ETH_USDT"])
                self.assertEqual(by_c["BTC_USDT"]["realised"], 7.0)
                self.assertEqual(by_c["ETH_USDT"]["realised"], -3.0)
                # 账户级读数不能被按币快照顶掉
                self.assertIsNone(store.last_pnl()["contract"])
                self.assertEqual(store.last_pnl()["realised"], 4.0)
                self.assertEqual(store.last_pnl("ETH_USDT")["realised"], -3.0)

    def test_legacy_db_gains_contract_column(self):
        """旧 paper 库（无 contract 列）打开后自动补列且不丢数据。"""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "account.db"
            conn = sqlite3.connect(str(db))
            conn.executescript(
                "CREATE TABLE pnl_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " snap_time INTEGER NOT NULL, equity REAL NOT NULL, unrealised REAL NOT NULL,"
                " realised REAL NOT NULL, drawdown REAL NOT NULL DEFAULT 0);"
                "INSERT INTO pnl_snapshot (snap_time, equity, unrealised, realised, drawdown)"
                " VALUES (1, 10.0, 0.0, 2.0, 0.0);")
            conn.commit()
            conn.close()
            from omnialpha.paper.store import PaperStore

            st = PaperStore(db)
            try:
                have = {r[1] for r in st._db().execute("PRAGMA table_info(pnl_snapshot)")}
                self.assertIn("contract", have)
                self.assertEqual(st.last_pnl()["realised"], 2.0, "旧行原样保留")
            finally:
                st.close()


if __name__ == "__main__":
    unittest.main()
