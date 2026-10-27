# -*- coding: utf-8 -*-
"""仓位口径：币数量（size_coins）与指定保证金（margin_usd）。

2026-10-28 用户要求：交易所下单面板支持 4 种输入（张 / 币 / USDT / 保证金），
系统原来只有 size(张) / size_usd(名义) / size_pct / margin_pct，缺两个「自然口径」：

  · `size_coins`  币数量：0.138 ETH / 0.5 SOL → 张 = 币 ÷ quanto（与价格无关）
  · `margin_usd`  指定保证金：名义 = 保证金 × leverage

本文件钉住：
  1. 币数量换算（含 quanto ≠ 1 的币：ETH 0.01 / XRP 10 / PEPE 1e7）；
  2. 保证金换算（必须乘 leverage，缺 leverage 退化为 1 倍名义）；
  3. 按步长 floor（宁可少下，不超授权），小于最小可下量 → 明确报错；
  4. 口径冲突（同时给两个）→ 拒单，绝不静默取一个。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta, GateApiError  # noqa: E402
from omnialpha.schema import SchemaError, parse_signal  # noqa: E402

PRICE = 2340.5


def meta(quanto=0.01, step=0.1, name="ETH_USDT") -> ContractMeta:
    return ContractMeta(name=name, quanto_multiplier=quanto, order_size_round=1,
                        order_price_round=0.01, leverage_max=50,
                        size_min=0 if step < 1 else 1, size_max=1e7,
                        enable_decimal=step < 1)


class _Client:
    def get_last_price(self, symbol):
        return PRICE

    def get_available_usdt(self):
        return 100.0


def _ex() -> Executor:
    import tempfile
    return Executor(_Client(), root=Path(tempfile.mkdtemp()), label_prefix="t", bot_id="t")


def _resolve(payload, m=None):
    intent = parse_signal(payload).intents[0]
    return _ex()._resolve_order_size(intent, m or meta())


class TestSizeCoins(unittest.TestCase):
    def test_eth_coins_to_contracts(self):
        """0.138 ETH ÷ 0.01 = 13.8 张（与真实持仓口径一致）。"""
        q, notional, basis = _resolve(
            {"action": "open_long", "symbol": "ETH_USDT", "size_coins": 0.138})
        self.assertEqual(basis, "size_coins")
        self.assertAlmostEqual(q, 13.8, places=8)
        self.assertAlmostEqual(notional, 13.8 * 0.01 * PRICE, places=4)

    def test_floors_to_step(self):
        """0.1387 ETH → 13.8 张（多余部分不下，绝不超过指定币量）。"""
        q, _, _ = _resolve({"action": "open_long", "symbol": "ETH_USDT",
                            "size_coins": 0.1387})
        self.assertAlmostEqual(q, 13.8, places=8)

    def test_xrp_quanto_10(self):
        """XRP 1 张 = 10 枚 → 25 枚 = 2.5 张。"""
        q, _, _ = _resolve({"action": "open_long", "symbol": "XRP_USDT",
                            "size_coins": 25}, meta(quanto=10.0, step=0.1))
        self.assertAlmostEqual(q, 2.5, places=8)

    def test_btc_integer_contract(self):
        """BTC 1 张 = 0.0001 → 0.0015 BTC = 15 张（整张）。"""
        q, _, _ = _resolve({"action": "open_long", "symbol": "BTC_USDT",
                            "size_coins": 0.0015}, meta(quanto=0.0001, step=1.0))
        self.assertEqual(q, 15.0)

    def test_below_min_unit_rejected(self):
        with self.assertRaises(GateApiError) as ctx:
            _resolve({"action": "open_long", "symbol": "ETH_USDT",
                      "size_coins": 0.0005})  # 0.05 张 < 0.1
        self.assertIn("SIZE_TOO_SMALL", str(ctx.exception))


class TestMarginUsd(unittest.TestCase):
    def test_margin_times_leverage(self):
        """100U 保证金 × 50x = 5000U 名义 → 5000/(2340.5×0.01) = 213.6 张。"""
        q, notional, basis = _resolve({"action": "open_long", "symbol": "ETH_USDT",
                                       "margin_usd": 100, "leverage": 50})
        self.assertEqual(basis, "margin_usd")
        self.assertAlmostEqual(q, 213.6, places=6)
        # 名义按「取整后的张数」算：213.6 × 0.01 × 2340.5（不是 5000，差的是被 floor 掉的 0.4 张）
        self.assertAlmostEqual(notional, 213.6 * 0.01 * PRICE, places=2)

    def test_margin_default_leverage_one(self):
        q, notional, _ = _resolve({"action": "open_long", "symbol": "ETH_USDT",
                                   "margin_usd": 100})
        self.assertAlmostEqual(q, 4.2, places=6)  # floor(100/23.405 = 4.27) → 4.2 张
        self.assertAlmostEqual(notional, 4.2 * 0.01 * PRICE, places=2)

    def test_negative_rejected(self):
        with self.assertRaises(SchemaError):
            parse_signal({"action": "open_long", "symbol": "ETH_USDT",
                          "margin_usd": -5})


class TestBasisConflict(unittest.TestCase):
    def test_two_bases_rejected(self):
        with self.assertRaises(GateApiError) as ctx:
            _resolve({"action": "open_long", "symbol": "ETH_USDT",
                      "size_coins": 0.138, "size_usd": 323})
        self.assertIn("SIZE_CONFLICT", str(ctx.exception))

    def test_coins_and_margin_rejected(self):
        with self.assertRaises(GateApiError) as ctx:
            _resolve({"action": "open_long", "symbol": "ETH_USDT",
                      "size_coins": 0.138, "margin_usd": 100, "leverage": 10})
        self.assertIn("SIZE_CONFLICT", str(ctx.exception))

    def test_none_rejected_by_schema(self):
        with self.assertRaises(SchemaError):
            parse_signal({"action": "open_long", "symbol": "ETH_USDT"})


if __name__ == "__main__":
    unittest.main()


class TestFloorLossIsBounded(unittest.TestCase):
    """币数量口径 floor 的「损失」必须**小于一个最小单位**，绝不能更多。"""

    def test_loss_less_than_one_unit(self):
        cases = (
            (0.1387, meta()),                       # ETH 单位 0.001
            (0.55, meta(quanto=1.0, step=0.1, name="SOL_USDT")),      # SOL 单位 0.1
            (2_500_000, meta(quanto=1e7, step=0.1, name="PEPE_USDT")),  # PEPE 单位 1e6
        )
        for coins, m in cases:
            q, _, _ = _resolve({"action": "open_long", "symbol": m.name,
                                "size_coins": coins}, m)
            got = q * m.quanto_multiplier
            unit = m.min_size_coins
            self.assertLessEqual(got, coins + 1e-12, "绝不能超过用户指定的币数量")
            self.assertLess(coins - got, unit + 1e-12,
                            f"{m.name}: 少下 {coins - got} 超过一个最小单位 {unit}")
