"""SL 覆盖上限（`_sl_cover_cap`）不得把「本轮即将撤掉的旧梯」算进敞口。

背景（martin-grid 产线实测 2026-10-14）：
- 阶梯是**逐档**调 `_open` 建仓的，每档都带 `intent.sl`；`_plan_shared_sl` 把同向同价
  的已有 SL 归并进来（新张数 = 旧同价之和 + 本档），并**钳制在 `_sl_cover_cap`**。
- `_sl_cover_cap` = 已成交持仓 + 已挂未成交的**同向**入场单。
- 而 `place_before_cancel`（先挂后撤）期间**新旧两梯同时在场** →
  上限被临时放大成「持仓 + 旧梯 + 新梯」→ 归并出来的 SL 多算**一整梯**。
  实测：持仓 5 + 挂单 26（=31 真实最大敞口）→ **SL 57**。
- 更糟的是它**卡住不回落**：管理轮（仅 modify_tp_sl）有「同价 no-op churn 守卫」不重挂；
  铺单轮的 cap 又是同样的放大值 → 57 一直留在盘上（产线 SL 账龄 >20min 仍是 57）。

修复：`execute_signal` 在**本轮确实要撤旧**（`replace_mode != "none"`）时，把快照里
（= 即将被 `_cancel_stale_owned` 整体撤掉的）旧普通单 id 预登记到
`_round_replaced_order_ids`；`_sl_cover_cap` 遍历挂单时跳过这些 id。
`_cancel_stale_owned` 撤的正是**整个旧快照**（`old_o` 逐条撤，`_should_keep_protection`
对非 reduce_only 一律返回 False）—— 所以排除它们是精确的，不是近似。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.paper.exchange import PaperExchange  # noqa: E402
from omnialpha.schema import Intent, SignalFile  # noqa: E402
from test_replace_no_empty_grid import _Feed, _Meta, _SYM  # noqa: E402


# ───────────────────────── 单元：cap 排除本轮将要撤掉的旧梯 ─────────────────────────

class _StubExec(Executor):
    """只注入 `_sl_cover_cap` 依赖的三处读接口，不起真交易所。"""

    def __init__(self, pos=0, orders=()):
        self.label_prefix = "mtg"
        self.order_scope = "own"
        self._pos = int(pos)
        self._orders = list(orders)
        self._round_replaced_order_ids = set()

    def _own_prefix(self, label=""):
        return "t-mtg"

    def _symbol_positions(self, symbol):
        return [{"contract": symbol, "side": "long", "size": str(self._pos)}] if self._pos else []

    def _owned_open_orders(self, symbol, prefix):
        return list(self._orders)

    @staticmethod
    def _text_owned(*a, **k):        # 未用到，保持签名完整
        return True


def _entry(oid, left, size=None):
    return {"id": str(oid), "status": "open", "size": size or left, "left": left,
            "text": "t-mtg"}


class TestCoverCapExcludesReplacedLadder(unittest.TestCase):
    def test_counts_all_pending_when_nothing_replaced(self):
        """replace=none 轮（旧梯留着）→ 全部挂单计入敞口（行为逐字不变）。"""
        ex = _StubExec(pos=5, orders=[_entry("o1", 10), _entry("o2", 16)])
        self.assertEqual(ex._sl_cover_cap(_SYM, "long"), 5 + 26)

    def test_excludes_orders_about_to_be_cancelled(self):
        """旧梯 26 张即将被撤 → 不计入上限（产线 57→31 的那 26 张）。"""
        ex = _StubExec(pos=5, orders=[_entry("old1", 10), _entry("old2", 16),
                                      _entry("new1", 10), _entry("new2", 16)])
        ex._round_replaced_order_ids = {"old1", "old2"}
        self.assertEqual(ex._sl_cover_cap(_SYM, "long"), 5 + 26,
                         "旧梯必须被排除；不排除就是 5+52=57（产线实测超配值）")

    def test_short_side_only_counts_short_entries(self):
        """只算同向：多头 SL 的上限不含卖单（另有独立的空头 SL）。"""
        ex = _StubExec(pos=0)
        ex._orders = [
            {"id": "b1", "status": "open", "size": 10, "left": 10, "text": "t-mtg"},
            {"id": "s1", "status": "open", "size": -7, "left": 7, "text": "t-mtg"},
        ]
        self.assertEqual(ex._sl_cover_cap(_SYM, "long"), 10)
        self.assertEqual(ex._sl_cover_cap(_SYM, "short"), 7)

    def test_finished_and_zero_left_ignored(self):
        ex = _StubExec(pos=3, orders=[
            _entry("a", 10),
            dict(_entry("b", 10), status="finished"),
            _entry("c", 0, size=10),
        ])
        self.assertEqual(ex._sl_cover_cap(_SYM, "long"), 13)


# ───────────────── 集成：铺单轮结束后 SL 张数 == 持仓 + 当前挂单张数 ─────────────────

def _grid_signal(prices):
    return SignalFile(
        intents=[Intent(action="open_long", symbol=_SYM, price=p, sl=90.0, tp=110.0,
                        size_usd=20.0, order_type="limit", leverage=20,
                        sl_mode="trigger", tp_mode="trigger",
                        sl_type="market", tp_type="market",
                        trigger_price_type="mark",
                        trigger_rule_sl=2, trigger_rule_tp=1)
                 for p in prices],
        replace="symbol",
    )


class TestSlSizeNotDoubledAcrossEntryRounds(unittest.TestCase):
    """两个连续的铺单轮（旧梯整梯替换）→ SL 只覆盖「持仓 + 新梯」，不叠加旧梯。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        tmp = Path(self._td.name)
        self.ex = PaperExchange(env="paper", store_path=tmp / "a.db", feed=_Feed(100.0),
                                config={"initial_capital": 10000, "leverage": 20})
        self.addCleanup(self.ex.store.close)
        self.executor = Executor(self.ex, label_prefix="mtg", root=tmp, bot_id="mg")

    def _pos(self):
        return sum(abs(int(p.get("size") or 0)) for p in self.ex.get_positions()
                   if p.get("contract") == _SYM)

    def _pending(self):
        return sum(int(o.get("left") or 0) for o in self.ex.list_orders(_SYM) or [])

    def _sl_size(self):
        """paper 的条件单是**扁平**字段（`text` / `size`），不是 Gate 的嵌套 `initial`。"""
        total = 0
        for p in self.ex.list_price_orders(_SYM) or []:
            text = str((p.get("initial") or {}).get("text") or p.get("text") or "")
            if text.rsplit("-", 1)[-1].lower() != "sl":
                continue
            total += abs(int(float(p.get("size") or 0)))
        return total

    def test_second_entry_round_does_not_double_sl(self):
        r1 = self.executor.execute_signal(_grid_signal([99.0, 98.7, 98.4]))
        self.assertTrue(r1.ok, r1)
        first = self._sl_size()
        self.assertGreater(first, 0, "R1 必须挂出 SL")
        self.assertEqual(first, self._pos() + self._pending(),
                         "R1：SL 应等于持仓 + 挂单")

        # R2：再次铺单（价格平移）→ 旧梯整梯替换。SL 不得因新旧同时在场面翻倍。
        r2 = self.executor.execute_signal(_grid_signal([98.2, 97.9, 97.6]))
        self.assertTrue(r2.ok, r2)
        self.assertEqual(
            self._sl_size(), self._pos() + self._pending(),
            f"R2 后 SL 超配：SL={self._sl_size()} 持仓={self._pos()} 挂单={self._pending()}"
            "（修复前这里会等于 持仓+旧梯+新梯）")
        self.assertEqual(self._sl_size(), first,
                         "两轮规模相同 → SL 张数不该变化（修复前会翻倍）")

    def test_replaced_ids_registered_only_when_replace_active(self):
        """结构护栏：登记只发生在 replace_mode != none，且登记的就是快照里的旧单。"""
        # R1：空挂单簿 → 无旧梯可撤 → 不登记（正确）
        self.executor.execute_signal(_grid_signal([99.0]))
        self.assertEqual(self.executor._round_replaced_order_ids, set(),
                         "首轮没有旧梯可撤，不该登记")

        # R2：铺单轮（replace=symbol）→ 登记 R1 那一批（即将被撤）
        before_ids = {str(o.get("id")) for o in self.ex.list_orders(_SYM) or []}
        self.executor.execute_signal(_grid_signal([98.7]))
        self.assertEqual(self.executor._round_replaced_order_ids, before_ids,
                         "铺单轮应登记上一轮的全部 owned 普通单")
        ids_now = {str(o.get("id")) for o in self.ex.list_orders(_SYM) or []}
        self.assertFalse(self.executor._round_replaced_order_ids & ids_now,
                         "登记集里不该有本轮新挂的单")

        # replace=none 轮（只改保护）→ 不登记，旧梯照留
        mgmt = SignalFile(intents=[Intent(action="modify_tp_sl", symbol=_SYM,
                                          tp=110.0, sl=90.0)], replace="none")
        self.executor.execute_signal(mgmt)
        self.assertEqual(self.executor._round_replaced_order_ids, set(),
                         "replace=none 的轮次不能登记（旧梯会留着，排除会造成保护不足）")


if __name__ == "__main__":
    unittest.main()
