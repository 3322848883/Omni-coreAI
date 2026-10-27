# -*- coding: utf-8 -*-
"""BOT 级阶梯补齐（ladder_template）：全局默认关闭，只补缺不撤不改。

背景：模型每轮入场腿数不稳定（实测 3/4/10 条），`replace` 整梯替换 →
阶梯层数随模型波动（10 层 → 3 层）。引擎按模板补齐缺档。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

TPL = {
    "enabled": True,
    "grid_levels": 3,
    "grid_lots": 1,
    "grid_step_pct": 1.0,
    "martin": [{"pct": 4.0, "lots": 6}],
}


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class C:
    def __init__(self, existing=None, mark=2000.0):
        self.existing = existing or []
        self.mark = mark
        self.placed = []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def list_orders(self, sym):
        return list(self.existing)

    def place_order(self, body):
        self.placed.append(body)
        return {"id": "new%d" % len(self.placed)}


class Report:
    def __init__(self):
        self.results = []


def _intent(price=1990.0):
    return Intent(action="open_long", symbol="ETH_USDT", price=price,
                  size_usd=price * 0.01, sl=1800.0)


class TestLadderTemplate(unittest.TestCase):
    def _ex(self, client, tpl=None):
        return Executor(client, symbols_whitelist=["ETH_USDT"],
                        ladder_template=(tpl if tpl is not None else TPL))

    def test_disabled_by_default(self):
        c = C()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])   # 不传模板
        rep = Report()
        ex._complete_ladder([_intent()], rep)
        self.assertEqual(c.placed, [], "全局默认必须关闭")
        self.assertEqual(rep.results, [])

    def test_fills_missing_levels(self):
        c = C(mark=2000.0)
        ex = self._ex(c)
        rep = Report()
        ex._complete_ladder([_intent()], rep)
        # 模板：网格 -1%/-2%/-3% → 1980/1960/1940，马丁 -4% → 1920
        prices = sorted(float(b["price"]) for b in c.placed)
        self.assertEqual(prices, [1920.0, 1940.0, 1960.0, 1980.0])
        sizes = {float(b["price"]): b["size"] for b in c.placed}
        self.assertEqual(sizes[1920.0], 6.0, "马丁档张数按模板")
        self.assertEqual(sizes[1980.0], 1.0, "网格档 1 张")
        det = rep.results[-1].detail
        self.assertEqual(len(det["placed"]), 4)
        self.assertEqual(det["anchor"], 2000.0)

    def test_skips_existing_levels(self):
        c = C(existing=[{"price": "1980", "size": 1}], mark=2000.0)
        ex = self._ex(c)
        rep = Report()
        ex._complete_ladder([_intent()], rep)
        prices = sorted(float(b["price"]) for b in c.placed)
        self.assertNotIn(1980.0, prices, "已存在的档不得重复挂")
        self.assertEqual(prices, [1920.0, 1940.0, 1960.0])

    def test_no_sl_no_completion(self):
        c = C(mark=2000.0)
        ex = self._ex(c)
        rep = Report()
        leg = Intent(action="open_long", symbol="ETH_USDT", price=1990.0,
                     size_usd=19.9, sl=None)
        ex._complete_ladder([leg], rep)
        self.assertEqual(c.placed, [], "没有 sl 的轮次不补齐（补齐必须带止损）")

    def test_no_entry_legs_no_completion(self):
        c = C(mark=2000.0)
        ex = self._ex(c)
        rep = Report()
        ex._complete_ladder([Intent(action="hold", symbol="ETH_USDT")], rep)
        self.assertEqual(c.placed, [])


if __name__ == "__main__":
    unittest.main()
