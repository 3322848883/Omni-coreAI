# -*- coding: utf-8 -*-
"""小数张精度：USDT ↔ 张数换算必须无二进制误差，且按**每个合约自己的步长**输出。

2026-10-28 实盘背景（pa-eth-v5）：
  · ETH_USDT `order_size_min = 0`（交易所对 15 个小数合约都返回 0），
    代码回退 0.1 —— 实测 0.15 通过、0.05 被拒，**0.1 就是真实步长**（不是猜错）。
  · 币的「很多位小数」体现在 `quanto_multiplier`（PEPE=1e7、XRP=10、BTC=0.0001），
    **不体现在张数步长**上：全市场只有两档 —— 步长 0.1（15 个小数字合约）
    与步长 1（其余 1051 个）。
  · 真正的硬限制在触发单端点：`/price_orders` 的 `initial.size` 是 int64，
    字符串 `"0.3"` / `"13.8"` / 浮点 `0.3` **全部 400**（2026-10-28 实测），
    所以小数张保护只能走 `auto_size`（见 test_protection_covers_whole_position.py）。

本文件钉住精度侧：
  1. 步长小数位推导（0.1→1、0.01→2、1→0、10→0）；
  2. `qty_to_wire` 输出已是步长整数倍、整张合约输出 int；
  3. `usd_to_contracts` / `quantize_size` 用 Decimal 取整，不再有 12.999999 尾巴；
  4. wire 层把 13.799999999999999 归一成 13.8（否则交易所 floor 成 13.7，每笔少 0.1 张）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.gate_client import ContractMeta, _wire_body  # noqa: E402
from omnialpha.sizing import (floor_to_step, qty_to_wire, quantize_size,  # noqa: E402
                              step_decimals, usd_to_contracts)


def meta(step: float, quanto: float = 0.01, name: str = "X") -> ContractMeta:
    return ContractMeta(name=name, quanto_multiplier=quanto, order_size_round=1,
                        order_price_round=0.01, leverage_max=50,
                        size_min=0 if step < 1 else 1, size_max=1e7,
                        enable_decimal=step < 1)


class TestStepDecimals(unittest.TestCase):
    def test_eth_like(self):
        self.assertEqual(step_decimals(0.1), 1)

    def test_finer_steps(self):
        self.assertEqual(step_decimals(0.01), 2)
        self.assertEqual(step_decimals(0.001), 3)

    def test_integer_steps(self):
        self.assertEqual(step_decimals(1), 0)
        self.assertEqual(step_decimals(10), 0)


class TestQtyToWire(unittest.TestCase):
    def test_decimal_contract_keeps_fraction(self):
        v = qty_to_wire(13.8, 0.1)
        self.assertEqual(v, 13.8)
        self.assertIsInstance(v, float)

    def test_decimal_contract_floors_to_step(self):
        self.assertEqual(qty_to_wire(13.87, 0.1), 13.8)
        self.assertEqual(qty_to_wire(13.89, 0.1), 13.8)

    def test_integer_contract_returns_int(self):
        v = qty_to_wire(13.8, 1)
        self.assertEqual(v, 13)
        self.assertIsInstance(v, int)

    def test_float_artifact_is_cleaned(self):
        """13.799999999999999（float 计算产物）必须被还原成 13.8。"""
        self.assertEqual(qty_to_wire(13.799999999999999, 0.1), 13.8)
        self.assertEqual(str(qty_to_wire(0.1 + 0.2, 0.1)), "0.3")


class TestDecimalSizing(unittest.TestCase):
    def test_usd_to_contracts_is_exact(self):
        """323 U / (2340.5 × 0.01) = 13.8 张，必须精确到 13.8（不是 13.7999…）。"""
        q = usd_to_contracts(323.0, 2340.5, meta(0.1))
        self.assertEqual(q, 13.8)
        self.assertEqual(str(q), "13.8")

    def test_usd_to_contracts_floors_not_rounds(self):
        """13.87 张的量 → 13.8（宁可少下，不可超名义）。"""
        q = usd_to_contracts(13.87 * 2340.5 * 0.01, 2340.5, meta(0.1))
        self.assertEqual(q, 13.8)

    def test_quantize_size_floor_no_tail(self):
        m = meta(0.1)
        self.assertEqual(quantize_size(13.8, m), 13.8)
        self.assertEqual(str(quantize_size(13.8, m)), "13.8")
        self.assertEqual(quantize_size(13.87, m), 13.8)

    def test_quantize_size_round_mode(self):
        m = meta(0.1)
        self.assertEqual(quantize_size(13.85, m, mode="round"), 13.9)
        self.assertEqual(quantize_size(13.84, m, mode="round"), 13.8)

    def test_btc_integer_contract(self):
        m = meta(1, quanto=0.0001, name="BTC_USDT")
        # 1 张 = 0.0001 BTC = 6 USD（价 60000）→ 60000U ≈ 10000 张
        self.assertEqual(usd_to_contracts(60000.0, 60000.0, m), 10000.0)
        self.assertEqual(qty_to_wire(3.9, 1), 3)
        # 100U / 6U = 16.66 张 → floor 16（整张合约不允许小数）
        self.assertEqual(usd_to_contracts(100.0, 60000.0, m), 16.0)
        self.assertIsInstance(qty_to_wire(16.0, 1), int)

    def test_pepe_huge_quanto(self):
        """PEPE quanto=1e7：步长仍是 0.1 张，换算要按 quanto 走。"""
        m = meta(0.1, quanto=1e7, name="PEPE_USDT")
        # 100 USDT / (0.00001 × 1e7) = 1 张
        self.assertEqual(usd_to_contracts(100.0, 0.00001, m), 1.0)
        self.assertEqual(qty_to_wire(1.05, 0.1), 1.0)


class TestWireNormalization(unittest.TestCase):
    def test_artifact_removed(self):
        self.assertEqual(_wire_body(13.799999999999999), 13.8)
        self.assertEqual(_wire_body(0.1 + 0.2), 0.3)

    def test_integral_float_becomes_int(self):
        self.assertIsInstance(_wire_body(8.0), int)
        self.assertEqual(_wire_body(8.0), 8)

    def test_nested_body(self):
        body = {"initial": {"size": -13.8, "price": "0"},
                "trigger": {"price": 2340.70}}
        out = _wire_body(body)
        self.assertEqual(out["initial"]["size"], -13.8)
        self.assertEqual(out["trigger"]["price"], 2340.7)
        self.assertEqual(body["initial"]["size"], -13.8, "不得原地修改调用方 dict")


if __name__ == "__main__":
    unittest.main()


class TestMinUnitInCoins(unittest.TestCase):
    """「最小单位」必须按**币口径**核对：交易所的 order_size_min 是「张」，
    人看的是币；两者关系 = size_step × quanto_multiplier。

    数据来源（2026-10-28 用带 `X-Gate-Size-Decimal: 1` 的合约接口逐个核对）：
      ETH  0.1 张 × 0.01   = 0.001 ETH          ← 与用户说法一致
      SOL  0.1 张 × 1      = 0.1 SOL            ← 与用户说法一致
      BTC  1 张   × 0.0001 = 0.0001 BTC
      XRP  0.1 张 × 10     = 1 XRP
      PEPE 0.1 张 × 1e7    = 1,000,000 PEPE
      DOGE 1 张   × 10     = 10 DOGE
    """

    TABLE = (
        ("ETH_USDT", 0.01, 0.1, True, 0.001),
        ("SOL_USDT", 1.0, 0.1, True, 0.1),
        ("BTC_USDT", 0.0001, 1.0, False, 0.0001),
        ("XRP_USDT", 10.0, 0.1, True, 1.0),
        ("PEPE_USDT", 1e7, 0.1, True, 1e6),
        ("DOGE_USDT", 10.0, 1.0, False, 10.0),
    )

    def test_min_unit_coins_table(self):
        for sym, quanto, step, dec, expect in self.TABLE:
            m = meta(step, quanto=quanto, name=sym)
            self.assertAlmostEqual(m.min_size_coins, expect, places=10,
                                   msg=f"{sym}: {m.size_step} 张 × {quanto} 应为 {expect}")

    def test_eth_user_claim(self):
        """用户口径：ETH 下单张数必须是 0.001 ETH 的整数倍。"""
        m = meta(0.1, quanto=0.01, name="ETH_USDT")
        self.assertAlmostEqual(m.min_size_coins, 0.001, places=12)
        # 13.8 张 = 0.138 ETH = 138 × 0.001 ✓
        self.assertAlmostEqual(13.8 * m.quanto_multiplier / m.min_size_coins, 138, places=9)

    def test_sol_user_claim(self):
        m = meta(0.1, quanto=1.0, name="SOL_USDT")
        self.assertAlmostEqual(m.min_size_coins, 0.1, places=12)

    def test_wire_size_is_multiple_of_min_unit(self):
        """任何输出到 wire 的张数，换算成币都必须是 min_size_coins 的整数倍。"""
        from decimal import Decimal
        for sym, quanto, step, dec, unit in self.TABLE:
            m = meta(step, quanto=quanto, name=sym)
            for raw in (13.87, 0.34, 2.29, 1.05):
                q = qty_to_wire(raw, m.size_step)
                coins = Decimal(str(q)) * Decimal(str(quanto))
                k = coins / Decimal(str(unit))
                self.assertEqual(k, k.to_integral_value(),
                                 f"{sym}: {q} 张 = {coins} 币 不是 {unit} 的整数倍")
