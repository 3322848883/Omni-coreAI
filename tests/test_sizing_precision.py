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
