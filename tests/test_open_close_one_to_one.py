# -*- coding: utf-8 -*-
"""开仓取整 → 平仓必须**一一对应**（不留残量）。

用户判据：「开仓时取整，那么平仓就应该可以一致，一一对应」。

本文件用**真实模拟盘引擎**（PaperExchange：走 lot/最小名义/价格校验，与实盘同源）
验证三件事，跨 5 个不同 quanto/步长的币种：

  1. 开多少张，持仓就是多少张（不做二次取整、不丢半步）
  2. 全平（size=0）→ 持仓归零，**精确 0**
  3. 指定张数平仓：给 q 平掉 q；给 q+余量 → 被交易所夹到 q（不会反向开仓）；
     只平一半 → 剩余恰好是 q − half（逐位相等，不是 float 近似）

只要这里红了，就说明「开仓 floor 过、平仓没对齐」——残仓的根源。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta  # noqa: E402
from omnialpha.paper.exchange import PaperExchange  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402

# (symbol, quanto, step, price)
COINS = (
    ("ETH_USDT", 0.01, 0.1, 2340.5),
    ("SOL_USDT", 1.0, 0.1, 168.42),
    ("BTC_USDT", 0.0001, 1.0, 61234.5),
    ("XRP_USDT", 10.0, 0.1, 2.3417),
    ("PEPE_USDT", 1e7, 0.1, 0.0000067),
)


class _Feed:
    """行情 + 合约元数据（离线）。"""

    name = "offline"

    def __init__(self):
        self.price = {}
        self.meta = {}
        for sym, quanto, step, px in COINS:
            self.price[sym] = px
            self.meta[sym] = ContractMeta(
                name=sym, quanto_multiplier=quanto, order_size_round=1,
                order_price_round=0.01, leverage_max=50,
                size_min=0 if step < 1 else 1, size_max=1e9,
                enable_decimal=step < 1)

    def get_last_price(self, symbol):
        return self.price[symbol]

    def get_orderbook_top(self, symbol, limit=5):
        p = self.price[symbol]
        return {"bids": [{"p": p, "s": 100}], "asks": [{"p": p, "s": 100}]}

    def get_ticker(self, symbol):
        p = self.price[symbol]
        return {"last": p, "mark_price": p, "funding_rate": 0.0}

    def get_contract(self, symbol):
        return self.meta[symbol]

    def get_klines(self, symbol, interval, limit=100):
        return []

    def get_contract_stats(self, symbol, limit=1):
        return []


class TestOpenCloseOneToOne(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.feed = _Feed()
        self.ex = PaperExchange(env="paper", store_path=self.tmp / "acc.db",
                                feed=self.feed,
                                config={"initial_capital": 100000, "leverage": 20})
        self.executor = Executor(self.ex, label_prefix="oc", root=self.tmp, bot_id="oc")

    def tearDown(self):
        try:
            self.ex.store.close()
        finally:
            self._td.cleanup()

    # ── 工具 ────────────────────────────────────────────
    def _qty(self, sym: str) -> D:
        for p in self.ex.get_positions() or []:
            if p.get("contract") == sym:
                return D(str(float(p.get("size") or 0)))
        return D(0)

    def _resolve(self, payload: dict, m) -> D:
        intent = parse_signal(payload).intents[0]
        q, _, _ = self.executor._resolve_order_size(
            intent, m, entry_hint=self.feed.price[payload["symbol"]])
        return D(str(q))

    def _open(self, sym: str, q: D) -> None:
        o = self.ex.place_order({"contract": sym, "size": float(q), "price": 0,
                                 "tif": "ioc", "type": "market", "text": "t-oc"})
        self.assertIn(str(o.get("status")), ("filled", "finished", "open"), o)

    # ── 1) 开多少就是多少 ───────────────────────────────
    def test_open_keeps_exact_quantity(self):
        for sym, quanto, step, px in COINS:
            with self.subTest(sym=sym):
                m = self.feed.meta[sym]
                q = self._resolve({"action": "open_long", "symbol": sym,
                                   "size_coins": float(D(str(step)) * 13 * D(str(quanto)))}, m)
                self._open(sym, q)
                self.assertEqual(self._qty(sym), q,
                                 f"{sym}: 开 {q} 张，持仓 {self._qty(sym)}")
                self.ex.close_position(sym, None, 0)     # 清场

    # ── 2) 全平归零 ─────────────────────────────────────
    def test_full_close_leaves_zero(self):
        for sym, quanto, step, px in COINS:
            with self.subTest(sym=sym):
                m = self.feed.meta[sym]
                q = self._resolve({"action": "open_long", "symbol": sym,
                                   "size_coins": float(D(str(step)) * 13 * D(str(quanto)))}, m)
                self._open(sym, q)
                self.ex.close_position(sym, None, 0)
                self.assertEqual(self._qty(sym), D(0), f"{sym}: 全平后残留 {self._qty(sym)}")

    # ── 3) 指定张数平仓：一一对应 / 超量夹取 / 半仓精确 ──
    def test_explicit_close_matches_open(self):
        for sym, quanto, step, px in COINS:
            with self.subTest(sym=sym):
                m = self.feed.meta[sym]
                q = self._resolve({"action": "open_long", "symbol": sym,
                                   "size_coins": float(D(str(step)) * 14 * D(str(quanto)))}, m)
                # 3a 平掉 q → 0
                self._open(sym, q)
                self.ex.close_position(sym, None, float(q))
                self.assertEqual(self._qty(sym), D(0), f"{sym}: 按 q 平仓后残留 {self._qty(sym)}")

                # 3b 给超量 q+5step → 夹到 q，不得反向
                self._open(sym, q)
                self.ex.close_position(sym, None, float(q + D(str(step)) * 5))
                self.assertEqual(self._qty(sym), D(0),
                                 f"{sym}: 超量平仓后残留 {self._qty(sym)}")

                # 3c 只平一半 → 剩余恰好 q - half
                self._open(sym, q)
                half = (D(str(step)) * 7)
                self.ex.close_position(sym, None, float(half))
                self.assertEqual(self._qty(sym), q - half,
                                 f"{sym}: 半仓后应为 {q - half}，实际 {self._qty(sym)}")
                self.ex.close_position(sym, None, 0)

    # ── 4) 四种口径开出来的仓，平仓口径一致（同一 q） ────
    def test_all_bases_yield_same_close_quantity(self):
        for sym, quanto, step, px in COINS:
            with self.subTest(sym=sym):
                m = self.feed.meta[sym]
                st, q0 = D(str(step)), D(str(quanto))
                q = st * 13
                coins = float(q * q0)
                usd = float(q * D(str(px)) * q0)
                lev = 50
                margin = float(q * D(str(px)) * q0 / D(lev))
                qs = {
                    "size": self._resolve({"action": "open_long", "symbol": sym,
                                           "size": float(q)}, m),
                    "size_coins": self._resolve({"action": "open_long", "symbol": sym,
                                                 "size_coins": coins}, m),
                    "size_usd": self._resolve({"action": "open_long", "symbol": sym,
                                               "size_usd": usd}, m),
                    "margin_usd": self._resolve({"action": "open_long", "symbol": sym,
                                                 "margin_usd": margin,
                                                 "leverage": lev}, m),
                }
                for name, got in qs.items():
                    self.assertEqual(got, q, f"{sym} {name}: {got} != {q}")
                    self._open(sym, got)
                    self.assertEqual(self._qty(sym), q,
                                     f"{sym} {name}: 持仓 {self._qty(sym)} != {q}")
                    self.ex.close_position(sym, None, 0)
                    self.assertEqual(self._qty(sym), D(0))


if __name__ == "__main__":
    unittest.main()
