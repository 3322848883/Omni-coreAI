"""整轮失败时的回滚：_rollback_newly_placed 的分支覆盖。

背景（2026-10-06 实测两次）：主循环任一腿失败就 break，之前成功的腿已挂在交易所上，
留下「缺腿的阶梯」（双向缺一侧 = 对冲不成立），且 report.ok=False 让「先挂后撤」也不执行，
新旧叠加。本测试钉住回滚的**每一条分支**：

  1. 本轮新挂的入场单        → 撤
  2. 快照里就有的入场单      → 不撤（上一轮的，不归本轮管）
  3. 本轮新挂的保护单        → 撤
  4. 快照里就有的保护单      → 不撤
  5. **正在保护真实持仓的保护单 → 绝不撤**（撤了就裸仓，比半成品严重）
  6. 持仓取不到              → 整段跳过撤价单（宁可不回滚，不冒裸仓风险）
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.executor import ExecReport, Executor
from omnialpha.schema import parse_signal


class _FakeClient:
    """最小桩：可配置的挂单/持仓，记录所有撤单调用。"""

    def __init__(self, orders=None, price_orders=None, positions=None, pos_error=False):
        self._orders = list(orders or [])
        self._price = list(price_orders or [])
        self._positions = list(positions or [])
        self._pos_error = pos_error
        self.cancelled_orders: list[str] = []
        self.cancelled_price: list[str] = []

    def banner(self):
        return ""

    def list_orders(self, contract=None):
        return list(self._orders)

    def list_price_orders(self, contract=None):
        return list(self._price)

    def get_positions(self):
        if self._pos_error:
            raise RuntimeError("positions unavailable")
        return list(self._positions)

    def cancel_order(self, oid):
        self.cancelled_orders.append(str(oid))

    def cancel_price_order(self, oid):
        self.cancelled_price.append(str(oid))


def _entry(oid, text="t-lad"):
    return {"id": oid, "text": text, "contract": "ETH_USDT", "size": 1, "left": 1}


def _prot(oid, text="t-lad-sl", reduce_only=True, size=-1):
    return {
        "id": oid,
        "trigger": {"price": "2600.0", "rule": 2},
        "initial": {"text": text, "size": size, "is_reduce_only": reduce_only,
                    "contract": "ETH_USDT"},
        "status": "open",
    }


def _intents():
    sig = parse_signal({"orders": [
        {"action": "open_long", "symbol": "ETH_USDT", "type": "limit",
         "price": 2600, "size_usd": 27, "sl": 2560, "tp": 2640},
    ]}, default_label="lad")
    from omnialpha.schema import expand_signal
    return expand_signal(sig)


class TestRollbackNewlyPlaced(unittest.TestCase):
    def _ex(self, client, td):
        return Executor(client, symbols_whitelist=["ETH_USDT"], root=Path(td),
                        bot_id="b1", label_prefix="lad", order_scope="own")

    def test_cancels_new_entry_and_new_protection(self):
        """分支 1+3：本轮新挂的入场单和保护单都撤掉。"""
        c = _FakeClient(orders=[_entry("E-new")], price_orders=[_prot("P-new")])
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(c, td)
            rep = ExecReport()
            ex._rollback_newly_placed({}, _intents(), rep)
        self.assertEqual(c.cancelled_orders, ["E-new"])
        self.assertEqual(c.cancelled_price, ["P-new"])
        self.assertTrue(any(r.action == "rollback" and r.ok for r in rep.results))

    def test_keeps_orders_present_in_snapshot(self):
        """分支 2+4：快照里就有的单（上一轮挂的）一个都不碰。"""
        c = _FakeClient(orders=[_entry("E-old")], price_orders=[_prot("P-old")])
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(c, td)
            rep = ExecReport()
            pre = {"ETH_USDT": {"prefix": "t-lad", "price_ids": {"P-old"},
                                "order_ids": {"E-old"}}}
            ex._rollback_newly_placed(pre, _intents(), rep)
        self.assertEqual(c.cancelled_orders, [])
        self.assertEqual(c.cancelled_price, [])

    def test_never_cancels_protection_of_live_position(self):
        """分支 5：保护真实持仓的单绝不撤 —— 撤了就裸仓。

        保护单方向约定：sell(-size) 平多、buy(+size) 平空（见 `_is_orphan_protector`）。
        持仓是空头 → 对应的保护单必须是 +size 的买单才「有对应持仓」。
        """
        c = _FakeClient(
            orders=[],
            price_orders=[
                _prot("P-live", "t-lad-sl", size=1),      # +1 买 → 平空仓 → 有对应持仓
                _prot("P-orphan", "t-lad-tp", size=-1),   # -1 卖 → 平多仓 → 无多仓 → 孤儿
            ],
            positions=[{"contract": "ETH_USDT", "size": -1, "mode": "dual_short"}],
        )
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(c, td)
            rep = ExecReport()
            ex._rollback_newly_placed({}, _intents(), rep)
        self.assertNotIn("P-live", c.cancelled_price,
                         "保护真实持仓的止损被撤了 —— 会裸仓")
        self.assertEqual(c.cancelled_price, ["P-orphan"],
                         "没有持仓可保护的孤儿保护单应当撤掉")

    def test_skips_price_cancel_when_positions_unavailable(self):
        """分支 6：持仓取不到 → 只撤入场单，不碰任何价单。"""
        c = _FakeClient(orders=[_entry("E-new")], price_orders=[_prot("P-new")],
                        pos_error=True)
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(c, td)
            rep = ExecReport()
            ex._rollback_newly_placed({}, _intents(), rep)
        self.assertEqual(c.cancelled_orders, ["E-new"])
        self.assertEqual(c.cancelled_price, [],
                         "持仓未知时撤价单可能撤掉持仓保护单 —— 必须整段跳过")
        self.assertTrue(any((r.detail or {}).get("refused") == "positions_unavailable"
                            for r in rep.results))

    def test_only_same_label_is_touched(self):
        """别的 bot 的单（不同 label 前缀）绝不碰。"""
        c = _FakeClient(orders=[_entry("E-mine", "t-lad"),
                                _entry("E-other", "t-brk")],
                        price_orders=[_prot("P-mine", "t-lad-tp"),
                                      _prot("P-other", "t-brk-sl")])
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(c, td)
            rep = ExecReport()
            ex._rollback_newly_placed({}, _intents(), rep)
        self.assertEqual(c.cancelled_orders, ["E-mine"])
        self.assertEqual(c.cancelled_price, ["P-mine"])


if __name__ == "__main__":
    unittest.main()
