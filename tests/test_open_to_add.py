# -*- coding: utf-8 -*-
"""`_map_open_to_add()` 的单元测试：有持仓时同侧 `open_*` → `add_*`（带暴露上限）。

动机：AI 说 `open_long` 而已有同侧持仓时，`_entry_gate` 会拒（防 new plan pile-up），
但 AI 的意图往往是「想更长」——拒掉等于白烧一轮（`POSITION_EXISTS` 占修复后残留失败的 28%）。

**风险与对策**：映射成 add 意味着重复/过期计划会每轮都加仓 → 仓位膨胀（单调累积、
不会自己回退）。所以只在「当前持仓名义 + 本单名义 ≤ 权益 × max_notional_pct」时才映射。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402


class Meta:
    quanto_multiplier = 0.0001


class AddClient:
    """size 是持仓张数（正=多、负=空）；equity 是账户权益。"""

    def __init__(self, size=22, equity=100.0, px=86000.0, account_error=False):
        self.size = size
        self.equity = equity
        self.px = px
        self.account_error = account_error

    def get_positions(self):
        if not self.size:
            return []
        mode = "dual_long" if self.size > 0 else "dual_short"
        return [{"contract": "BTC_USDT", "size": self.size, "mode": mode}]

    def get_account(self):
        if self.account_error:
            raise RuntimeError("account: down")
        return {"total": self.equity, "available": self.equity}

    def get_contract(self, symbol):
        return Meta()

    def get_last_price(self, symbol):
        return self.px


class TestMapOpenToAdd(unittest.TestCase):
    def _ex(self, client, pct=5.0):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Executor(client, position_policy="manage_only",
                        account_risk={"max_notional_pct": pct},
                        root=Path(td.name), bot_id="t")

    def _intent(self, action="open_long", size_usd=100.0):
        return Intent(action=action, symbol="BTC_USDT", size_usd=size_usd,
                      meta={"requested_action": action})

    def test_maps_when_within_exposure_cap(self):
        # 持仓 22 张 × 0.0001 × 86000 ≈ 189 名义；权益 100 × 5 = 500 上限；加 100 → 289 ≤ 500
        ex = self._ex(AddClient(size=22, equity=100.0))
        it = self._intent()
        self.assertEqual(ex._map_open_to_add(it), "")
        self.assertEqual(it.meta["requested_action"], "add_long")
        self.assertEqual(it.meta["mapped_from"], "open_long")

    def test_refuses_when_over_exposure_cap(self):
        """超上限就退回拒绝 —— 防堆积保护重新生效（否则每轮加仓会无限膨胀）。"""
        ex = self._ex(AddClient(size=22, equity=100.0))
        it = self._intent(size_usd=400.0)          # 189 + 400 > 500
        note = ex._map_open_to_add(it)
        self.assertIn("防仓位膨胀", note)
        self.assertEqual(it.meta["requested_action"], "open_long", "不应改写 meta")

    def test_short_side_maps_to_add_short(self):
        ex = self._ex(AddClient(size=-22, equity=100.0))
        it = self._intent(action="open_short", size_usd=100.0)
        self.assertEqual(ex._map_open_to_add(it), "")
        self.assertEqual(it.meta["requested_action"], "add_short")

    def test_opposite_side_not_mapped(self):
        """空头持仓上发 open_long → 不该映射（应先 close/reduce，方向反了）。"""
        ex = self._ex(AddClient(size=-22, equity=100.0))
        it = self._intent(action="open_long")
        self.assertIn("不同侧", ex._map_open_to_add(it))
        self.assertEqual(it.meta["requested_action"], "open_long")

    def test_no_position_not_mapped(self):
        ex = self._ex(AddClient(size=0))
        self.assertEqual(ex._map_open_to_add(self._intent()), "无持仓")

    def test_account_unavailable_not_mapped(self):
        """算不出暴露上限就不映射（保守：宁可白烧一轮，也不冒险加仓）。"""
        ex = self._ex(AddClient(account_error=True))
        self.assertIn("无法计算", ex._map_open_to_add(self._intent()))

    def test_already_add_is_allowed(self):
        ex = self._ex(AddClient(size=22, equity=100.0))
        it = self._intent(action="open_long")
        it.meta["requested_action"] = "add_long"
        self.assertEqual(ex._map_open_to_add(it), "")


if __name__ == "__main__":
    unittest.main()
