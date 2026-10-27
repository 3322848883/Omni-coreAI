# -*- coding: utf-8 -*-
"""0 张挂单兜底闸门（2026-10-28 实测事故回归）。

事故：19:02 的计划被重放执行，7 条 23~24U 的网格腿以 size=0 挂到了交易所
（text=t-mtg / status=open / left=0）。`_resolve_order_size` 自身不会返回 0，
所以这是**防御性**闸门 —— 但必须证明：一旦上游给出 0，下单前就被拒。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402


class Meta:
    name = "ETH_USDT"
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    enable_decimal = True
    size_max = 0
    order_price_round = 0.05
    mark_price = 2331.5


class FakeClient:
    def __init__(self):
        self.placed = []
        self.position_mode = "dual"

    def get_position_mode(self):
        return self.position_mode

    def place_order(self, body):
        self.placed.append(body)
        return {"id": "x", "size": body.get("size")}

    def get_contract(self, symbol):
        return Meta()

    def set_leverage(self, symbol, lev):
        return {"leverage": lev}


class TestZeroSizeGuard(unittest.TestCase):
    def test_zero_contracts_is_refused(self):
        c = FakeClient()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        ex._resolve_order_size = lambda *a, **k: (0.0, 0.0, "size_usd")  # 模拟上游回归
        r = ex._open(Intent(action="open_long", symbol="ETH_USDT", size_usd=23.9, sl=2165.0,
                            order_type="limit", price=2331.5))
        self.assertFalse(r.ok)
        self.assertIn("SIZE_ZERO", r.error)
        self.assertEqual(c.placed, [], "0 张单绝不能发到交易所")

    def test_negative_contracts_is_refused(self):
        c = FakeClient()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        ex._resolve_order_size = lambda *a, **k: (-1.0, -10.0, "size_usd")
        r = ex._open(Intent(action="open_long", symbol="ETH_USDT", size_usd=23.9, sl=2165.0,
                            order_type="limit", price=2331.5))
        self.assertFalse(r.ok)
        self.assertIn("SIZE_ZERO", r.error)
        self.assertEqual(c.placed, [])

    # 正常路径（size_usd → 1 张 → 下单）由 test_size_bases_* / test_sizing_* 覆盖，
    # 这里只钉「0/负张必须在下单前被拒」两条。


if __name__ == "__main__":
    unittest.main()
