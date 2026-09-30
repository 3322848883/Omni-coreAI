# -*- coding: utf-8 -*-
"""纸面盘孤儿保护单清理端到端测试。

场景：开多仓 + 挂 TP/SL → 交易所侧 TP 触发平仓 → SL 变孤儿 → 清理应撤掉它。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gate_bot.executor import Executor


class PaperClient:
    """极简纸面客户端：模拟 Gate price_orders 语义（含 is_reduce_only 字段）。"""

    def __init__(self):
        self.positions = []
        self.price_orders = []
        self._seq = 1000
        self.cancelled = []

    def get_account(self):
        return {"total": "10000", "available": "10000", "balance": "10000"}

    def get_positions(self):
        return list(self.positions)

    def list_price_orders(self, symbol=None):
        return [dict(o) for o in self.price_orders]

    def cancel_price_order(self, pid):
        self.cancelled.append(str(pid))
        self.price_orders = [o for o in self.price_orders if str(o.get("id")) != str(pid)]
        return True

    def place_protector(self, text: str, size: float, trigger_price: float,
                        is_reduce_only: bool = True):
        """挂一张保护单（模拟 Gate 返回结构）。"""
        self._seq += 1
        po = {
            "id": self._seq,
            "status": "open",
            "text": text,
            "size": size,
            "initial": {
                "contract": "BTC_USDT",
                "size": size,
                "text": text,
                "is_reduce_only": is_reduce_only,
                "is_close": True,
            },
            "trigger": {"price": trigger_price, "rule": 1 if size > 0 else 2},
        }
        self.price_orders.append(po)
        return po

    def set_position(self, size: float):
        if size == 0:
            self.positions = []
        else:
            self.positions = [{"contract": "BTC_USDT", "size": size, "entry_price": 83000}]


class TestOrphanCleanupE2E(unittest.TestCase):
    def _make(self, root: Path):
        client = PaperClient()
        ex = Executor(
            client,
            label_prefix="pt",
            root=root,
            bot_id="paper-test",
        )
        return client, ex

    def test_tp_fill_leaves_sl_orphan_and_cleanup_removes(self):
        """TP 触发平仓后，SL 变孤儿 → 清理撤掉。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            # 1) 开多仓 + 双保护
            client.set_position(10)  # 多仓
            tp = client.place_protector("t-pt-tp", -10, 84000)   # 卖平多
            sl = client.place_protector("t-pt-sl", -10, 82000)   # 卖平多

            # 有仓 → 两张都不是孤儿
            self.assertFalse(ex._is_orphan_protector(tp, client.get_positions()))
            self.assertFalse(ex._is_orphan_protector(sl, client.get_positions()))

            # 2) 模拟 TP 触发：持仓归零，TP 消失，SL 残留
            client.set_position(0)
            client.price_orders = [sl]  # 只剩 SL

            # 3) SL 现在是孤儿
            self.assertTrue(ex._is_orphan_protector(sl, client.get_positions()))

            # 4) 清理应撤掉
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(cleaned, [str(sl["id"])])
            self.assertEqual(client.price_orders, [])
            self.assertEqual(client.cancelled, [str(sl["id"])])

    def test_both_orphans_when_flat(self):
        """无持仓时 TP+SL 都是孤儿。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(0)
            tp = client.place_protector("t-pt-tp", -10, 84000)
            sl = client.place_protector("t-pt-sl", -10, 82000)
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(len(cleaned), 2)
            self.assertEqual(client.price_orders, [])

    def test_protectors_kept_when_position_exists(self):
        """有对应持仓 → 保护单保留。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)  # 多仓
            tp = client.place_protector("t-pt-tp", -10, 84000)
            sl = client.place_protector("t-pt-sl", -10, 82000)
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(cleaned, [])
            self.assertEqual(len(client.price_orders), 2)  # 都保留

    def test_short_position_protectors_kept(self):
        """空仓保护单（buy 平空）在空仓存在时保留。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(-8)  # 空仓
            tp = client.place_protector("t-pt-tp", 8, 82000)   # 买平空
            sl = client.place_protector("t-pt-sl", 8, 84000)
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(cleaned, [])
            self.assertEqual(len(client.price_orders), 2)

    def test_other_bot_namespace_untouched(self):
        """别家命名空间的单不碰。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))  # label_prefix=pt
            client.set_position(0)
            other = client.place_protector("t-smc-tp", -2, 84610)  # smc 命名空间
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(cleaned, [])  # 不碰别家
            self.assertEqual(len(client.price_orders), 1)

    def test_entry_order_not_touched(self):
        """入场单（非 tp/sl 后缀）不算保护单。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(0)
            entry = client.place_protector("t-pt-entry", 10, 83000)
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(cleaned, [])
            self.assertEqual(len(client.price_orders), 1)

    def test_cross_bot_orphan_not_cleaned_by_wrong_prefix(self):
        """同上：smc 的单不会被 brk 清理器清掉。"""
        with tempfile.TemporaryDirectory() as td:
            client = PaperClient()
            ex = Executor(client, label_prefix="brk", root=Path(td), bot_id="brooks-btc")
            client.set_position(0)
            smc = client.place_protector("t-smc-tp", -2, 84610)
            self.assertTrue(ex._is_orphan_protector(smc, []))  # 是孤儿
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(cleaned, [])  # 但不在命名空间内 → 不清


if __name__ == "__main__":
    unittest.main()
