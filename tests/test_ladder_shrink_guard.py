# -*- coding: utf-8 -*-
"""缩梯防护（2026-10-28 实测事故回归）。

事故：19:29 轮模型只规划 3 条腿（1 网格 + 2 马丁），`replace: symbol` 的 stale 清理
把旧 7 条网格档全撤 → 盘上阶梯从 10 层塌到 3 层，随之 TP/SL 尺寸漂移。

规则：只撤与本轮新梯档位**重合（±0.6%）**的旧入场单；价格不重合的档位保留。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor, _ENTRY_ACTIONS  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402


class C:
    def __init__(self, orders):
        self._orders = orders
        self.cancelled = []

    def _owned_open_orders(self, symbol, prefix):
        return [o for o in self._orders if str(o.get("text", "")).startswith(prefix or "")]

    def _owned_price_orders(self, symbol, prefix):
        return []

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {"id": oid}

    def cancel_price_order(self, pid):
        return {"id": pid}

    def list_price_orders(self, symbol):
        return []

    def list_orders(self, symbol):
        return []


class Report:
    def __init__(self):
        self.results = []


class TestLadderShrinkGuard(unittest.TestCase):
    def _run(self, old_orders, new_prices):
        c = C(old_orders)
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], order_scope="own",
                      default_replace="symbol", ladder_shrink_guard=True)
        ex._positions_or_error = lambda: ([], None)
        ex._has_pending_entry = lambda sym: True
        ex._owned_open_orders = lambda sym, prefix: [o for o in old_orders
                                                     if str(o.get("text", "")).startswith(prefix or "")]
        ex._should_keep_protection = lambda *a, **k: False
        pre_owned = {"ETH_USDT": {
            "prefix": "t-mtg",
            "order_ids": {str(o["id"]) for o in old_orders},
            "price_ids": set(),
        }}
        intents = [Intent(action="open_long", symbol="ETH_USDT", size_usd=22.9,
                          price=p, sl=2165.0) for p in new_prices]
        rep = Report()
        ex._cancel_stale_owned(pre_owned, intents, rep)
        return c.cancelled, rep

    def test_non_overlapping_olds_are_kept(self):
        olds = [
            {"id": "1", "price": "2330.5", "text": "t-mtg"},
            {"id": "2", "price": "2320", "text": "t-mtg"},
            {"id": "3", "price": "2308.5", "text": "t-mtg"},
            {"id": "4", "price": "2281", "text": "t-mtg"},
        ]
        cancelled, rep = self._run(olds, [2295.0, 2248.5, 2202.0])
        self.assertEqual(cancelled, [], "价格不重合的旧档不能被撤（缩梯）")
        det = rep.results[0].detail
        self.assertEqual(sorted(det["ladder_shrink_guarded"]), ["1", "2", "3", "4"])

    def test_overlapping_old_is_replaced(self):
        olds = [
            {"id": "1", "price": "2295", "text": "t-mtg"},   # 与新档重合 ±0.6%
            {"id": "2", "price": "2281", "text": "t-mtg"},   # 不重合
        ]
        cancelled, rep = self._run(olds, [2295.0])
        self.assertEqual(cancelled, ["1"], "重合档应被新档替换（不堆叠）")
        self.assertEqual(rep.results[0].detail["ladder_shrink_guarded"], ["2"])

    def test_guard_off_by_default(self):
        """默认关闭：既有 replace 契约不变（test_replace_no_empty_grid）。"""
        olds = [{"id": "1", "price": "2330.5", "text": "t-mtg"}]
        c = C(olds)
        ex = Executor(c, symbols_whitelist=["ETH_USDT"], order_scope="own",
                      default_replace="symbol")
        ex._positions_or_error = lambda: ([], None)
        ex._has_pending_entry = lambda sym: True
        ex._should_keep_protection = lambda *a, **k: False
        ex._owned_open_orders = lambda sym, prefix: list(olds)
        rep = Report()
        ex._cancel_stale_owned({"ETH_USDT": {"prefix": "t-mtg",
                                             "order_ids": {"1"}, "price_ids": set()}},
                               [Intent(action="open_long", symbol="ETH_USDT",
                                       size_usd=22.9, price=2202.0, sl=2165.0)], rep)
        self.assertEqual(c.cancelled, ["1"], "开关关闭时必须照旧替换")

    def test_entry_actions_constant(self):
        self.assertIn("open_long", _ENTRY_ACTIONS)
        self.assertIn("add_long", _ENTRY_ACTIONS)
        self.assertNotIn("modify_tp_sl", _ENTRY_ACTIONS)


if __name__ == "__main__":
    unittest.main()
