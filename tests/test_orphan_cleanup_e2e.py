# -*- coding: utf-8 -*-
"""纸面盘孤儿保护单清理端到端测试。

场景：开多仓 + 挂 TP/SL → 交易所侧 TP 触发平仓 → SL 变孤儿 → 清理应撤掉它。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor


class PaperClient:
    """极简纸面客户端：模拟 Gate price_orders 语义（含 is_reduce_only 字段）。"""

    def __init__(self):
        self.positions = []
        self.price_orders = []
        self.open_orders = []
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

    def list_orders(self, symbol=None):
        return [dict(o) for o in self.open_orders]

    def place_entry(self, text: str, size: float, left=None):
        """挂一张未成交的入场单（非 reduce-only）。"""
        self._seq += 1
        o = {
            "id": self._seq,
            "text": text,
            "size": size,
            "left": size if left is None else left,
            "is_reduce_only": False,
            "status": "open",
        }
        self.open_orders.append(o)
        return o

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

    def test_pending_entry_blocks_orphan_sweep(self):
        """待成交入场单的预挂 TP/SL 不能被当孤儿撤掉（否则成交即裸仓）。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(0)                 # 还没成交 → 无持仓
            client.place_entry("t-pt", 11)         # 入场限价单
            client.place_protector("t-pt-tp", -6, 86900)
            client.place_protector("t-pt-sl", -11, 85300)
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(cleaned, [])
            self.assertEqual(len(client.price_orders), 2)   # 保护单保留
            self.assertEqual(client.cancelled, [])

    def test_entry_fully_filled_does_not_block(self):
        """入场单已全部成交（left=0）→ 不再豁免，孤儿照常清理。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(0)
            client.place_entry("t-pt", 11, left=0)
            client.place_protector("t-pt-sl", -11, 85300)
            self.assertEqual(len(ex._cleanup_orphan_protectors("BTC_USDT")), 1)

    def test_other_bot_pending_entry_does_not_block(self):
        """别家命名空间的待成交入场单不豁免本 bot 的清理。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))       # label_prefix=pt
            client.set_position(0)
            client.place_entry("t-smc", 11)         # 别家的
            client.place_protector("t-pt-sl", -11, 85300)
            self.assertEqual(len(ex._cleanup_orphan_protectors("BTC_USDT")), 1)


    def test_pending_stop_entry_blocks_sweep(self):
        """`stop_entry_*` 是**条件单**（挂在 price_orders），也必须算「待成交入场」。

        只扫普通挂单会漏掉它 → 无持仓时把它的预挂保护单当孤儿撤掉 →
        委托一成交就是裸仓。线上实测 2026-10-02 16:12/16:17/16:28 连续三次发生，
        当时 mark 距触发只差 0.17%。
        """
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(0)                      # 还没成交 → 无持仓
            # 突破进场单：条件单、非 reduce_only、text 无 tp/sl 后缀
            client.place_protector("t-pt", -39, 85050, is_reduce_only=False)
            # 为它预挂的保护单
            client.place_protector("t-pt-sl", 39, 85480)
            client.place_protector("t-pt-tp", 19, 84200)
            self.assertTrue(ex._has_pending_entry("BTC_USDT"),
                            "条件单形态的待成交入场单必须被识别")
            cleaned = ex._cleanup_orphan_protectors("BTC_USDT")
            self.assertEqual(cleaned, [], "有待成交条件单时不该撤保护单")
            self.assertEqual(len(client.price_orders), 3, "三张单都该保留")

    def test_finished_stop_entry_does_not_block(self):
        """已终结（cancelled/finished）的条件单不算待成交。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(0)
            e = client.place_protector("t-pt", -39, 85050, is_reduce_only=False)
            e["status"] = "cancelled"
            self.assertFalse(ex._has_pending_entry("BTC_USDT"))

    def test_reduce_only_condition_not_treated_as_entry(self):
        """reduce_only 的条件单是保护单，不是入场单。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(0)
            client.place_protector("t-pt-sl", 39, 85480)   # reduce_only 默认 True
            self.assertFalse(ex._has_pending_entry("BTC_USDT"))


if __name__ == "__main__":
    unittest.main()
