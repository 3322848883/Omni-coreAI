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
        self.cancelled = []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def list_orders(self, sym):
        return list(self.existing)

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.existing = [o for o in self.existing if str(o.get("id")) != str(oid)]
        return {"id": oid}

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

    def test_no_extra_layers_when_already_full(self):
        """已存在入场单 ≥ 模板总档数 → 不得叠加（防模型档 + 模板档变成 14 层）。

        2026-10-30 语义收紧：原来靠「条数够就跳过」实现，那会让**档距变了的旧梯
        永远换不掉**（实测部署后卡在 step 0.445% 的旧档位）。现在模板是唯一权威：
        与模板价不重合的旧档先撤、再补成模板档，最终条数仍等于模板档数 ——
        防叠加的目标不变，断言改看「最终条数」而不是「走哪个分支」。
        """
        olds = [{"id": f"o{i}", "price": str(px), "size": 1}
                for i, px in enumerate((1990, 1985, 1975, 1960))]
        c = C(existing=olds, mark=2000.0)
        ex = self._ex(c)
        rep = Report()
        ex._complete_ladder([_intent()], rep)
        # 1960 与模板档完全重合 → 保留；其余 3 条不合规被撤 → 补 3 条 = 总共 4 档
        final = sorted(float(b["price"]) for b in c.placed) + \
            sorted(float(o["price"]) for o in c.existing)
        self.assertEqual(len(final), 4,
                         f"最终档数必须等于模板档数（不叠加），实际 {len(final)}")
        self.assertEqual(sorted(final), [1920.0, 1940.0, 1960.0, 1980.0])

    def test_prunes_off_template_levels(self):
        """模板启用时是唯一权威：价格不在模板档位上的入场单被撤掉。

        实测：清理到 10 层后模型下一轮又把 2211.5×9 / 2223.5×1 铺回来 → 只靠补缺不收敛。
        """
        # 模板：1980/1960/1940 + 马丁 1920；库里多了一条 1930（距最近模板档 0.52% > 0.3%）
        olds = [{"id": "keep1", "price": "1980", "size": 1},
                {"id": "bad", "price": "1930", "size": 9}]
        c = C(existing=olds, mark=2000.0)
        ex = self._ex(c)
        rep = Report()
        ex._complete_ladder([_intent()], rep)
        self.assertIn("bad", c.cancelled, "非模板档必须撤销")
        self.assertNotIn("keep1", c.cancelled, "模板档必须保留")
        pruned = [r.detail for r in rep.results if r.detail.get("pruned_off_template")]
        self.assertEqual(pruned[0]["pruned_off_template"][0]["price"], 1930.0)

    def test_dedupes_same_price_orders(self):
        """同价重复档（模型与补档同轮抢挂）只保留一条。

        2026-10-30：档位归属容差从 0.3% 收紧到 0.02% 后，1980.5 已经落在
        模板价 1980 的容差外 → 由 prune 直接撤掉（不再等 dedupe 段）。
        结论不变（只留一条），但要走当前路径，所以断言改成「保留的是与模板价
        完全一致的那条」。
        """
        olds = [{"id": "dupA", "price": "1980", "size": 1},
                {"id": "dupB", "price": "1980.5", "size": 1}]
        c = C(existing=olds, mark=2000.0)
        ex = self._ex(c)
        rep = Report()
        ex._complete_ladder([_intent()], rep)
        self.assertEqual(len(c.cancelled), 1, f"同价两条必须撤掉一条：{c.cancelled}")
        self.assertEqual(c.cancelled, ["dupB"],
                         "应保留与模板价 1980 完全重合的那条")

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
