"""「撤单腿失去承接物」剥离 —— 网格空窗复发（破坏性半执行）回归测试。

## 产线事故（2026-10-14 16:26:36，martin-grid）

LLM 的一轮计划是「**撤旧梯 + 重铺 10 层**」：

```
plan_reasoning: "4h/1h/15m均discount,4h偏空仅记录;旧梯首档距现价仅6点不合规,重铺10层"
orders[0]: cancel_all(ETH_USDT)   reasoning="撤旧阶梯(首档距现价仅6点)"
orders[1]: modify_tp_sl(ETH_USDT)
orders[2..11]: open_long × 10      ← confidence 全为 0.0
```

风控（`min_confidence=0.55`、`drop_on_low_conf=False`）把 10 条入场腿**降级成 hold**
（不是丢弃，见 `risk.apply_risk`），bridge 再把「无 tp/sl 的 hold」丢掉 →
落到盘上的 signal 只剩 `cancel_all + modify_tp_sl`，`replace=none`。

而 executor 的 `place_before_cancel` 把 `cancel_all` 转成「撤掉快照差集」，
`_cancel_stale_owned` 的守卫又把「本轮有显式撤单意图」当作承接物放行 →
**整梯 10 张被撤光、零替补**，直到 16:47:53 下一轮才重铺（**空窗 ≈18 分钟**）。

本质：**破坏性的一半执行了，建设性的一半被风控砍掉**。

## 修复（契约层 bridge.chips_to_signal）

判据：**计划里本来有入场腿、但通过风控的入场腿一条都没有** →
撤单腿是在为「重铺」让位，而重铺没发生 → 剥掉撤单腿。

必须保留撤单的反例（都锁在测试里）：
 - 计划里**本就没有**入场腿（AI 单纯想清空挂单簿）→ 撤单就是意图本身，照发；
 - 计划里的入场腿通过了 → 撤单是为重铺让位，照发（此时 replace=symbol）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from omnialpha.executor import _INTENT_PRIORITY, Executor  # noqa: E402
from omnialpha.paper.exchange import PaperExchange  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402
from omnialpha.strategist.bridge import (  # noqa: E402
    _CANCEL_ACTIONS, _ENTRY_ACTIONS, chips_to_signal,
)
from omnialpha.strategist.risk import RiskConfig, apply_risk  # noqa: E402
from omnialpha.strategist.schema import Chip, Plan  # noqa: E402
from test_replace_no_empty_grid import _Feed, _Meta, _SYM  # noqa: E402

# 与 martin-grid 产线同参（config/bots/martin-grid.yaml 的 strategist.risk）
PROD_RISK = RiskConfig(min_confidence=0.55, max_chips=32, max_notional_usd=1200.0)


def _plan(entry_conf, symbol=_SYM, n_entries=10, with_cancel=True, with_modify=True):
    chips = []
    if with_cancel:
        chips.append(Chip(symbol=symbol, action="cancel_all", confidence=0.62,
                          reasoning="撤旧阶梯(首档距现价仅6点)"))
    if with_modify:
        chips.append(Chip(symbol=symbol, action="modify_tp_sl", confidence=0.62,
                          tp=110.0, sl=90.0, reasoning="持仓5张TP/SL"))
    for i in range(n_entries):
        chips.append(Chip(symbol=symbol, action="open_long", confidence=entry_conf,
                          size_usd=100.0, price=99.0 - i * 0.3,
                          sl=90.0, tp=110.0, order_type="limit",
                          leverage=20, reasoning=f"档{i}"))
    return Plan(cycle_id="c-prod", reasoning="旧梯不合规,重铺10层", chips=chips)


def _signal(plan, risk=PROD_RISK):
    return chips_to_signal(plan, apply_risk(plan, risk), bot_id="mg")


class TestCancelSetInSync(unittest.TestCase):
    def test_cancel_actions_match_executor_priority3(self):
        """bridge 的撤单集合必须与 executor 的第 3 类（撤单）优先级表同源。"""
        expected = {a for a, p in _INTENT_PRIORITY.items() if p == 3}
        self.assertEqual(set(_CANCEL_ACTIONS), expected)


class TestOrphanedCancelDropped(unittest.TestCase):
    """撤单腿失去承接物时被剥离（bridge 层，走真实风控）。"""

    def test_entries_rejected_cancel_is_dropped(self):
        """产线场景复现：10 条入场腿 confidence 0.0 全被降级 → 撤单腿必须被剥掉。"""
        out = _signal(_plan(entry_conf=0.0))
        acts = [o["action"] for o in out["orders"]]
        self.assertNotIn("cancel_all", acts,
                         "入场腿全被风控砍掉后，撤单腿不能再单独发出去（会撤空网格）")
        self.assertEqual(acts, ["modify_tp_sl"])
        self.assertEqual(out["replace"], "none")
        self.assertTrue(out["meta"]["dropped_cancels"],
                        "剥离必须留痕（dropped_cancels）")
        self.assertTrue(any("剥掉撤单腿" in n for n in out["meta"]["risk_notes"]),
                        "剥离必须在 risk_notes 里可见")

    def test_entries_pass_cancel_is_kept(self):
        """对照：入场腿通过风控 → 撤单腿照发，replace=symbol（新梯接替旧梯）。"""
        out = _signal(_plan(entry_conf=0.9))
        acts = [o["action"] for o in out["orders"]]
        self.assertIn("cancel_all", acts)
        self.assertEqual(out["replace"], "symbol")
        self.assertEqual(out["meta"]["dropped_cancels"], [])

    def test_pure_cancel_plan_is_kept(self):
        """对照：计划里本就没有入场腿（只想清空挂单簿）→ 撤单就是意图本身，照发。"""
        out = _signal(_plan(entry_conf=0.0, n_entries=0))
        self.assertIn("cancel_all", [o["action"] for o in out["orders"]])
        self.assertEqual(out["meta"]["dropped_cancels"], [])

    def test_cancel_for_other_symbol_not_dropped(self):
        """精确性：入场腿属于 ETH，撤单属于 BTC → BTC 的撤单不该被牵连剥掉。"""
        plan = Plan(cycle_id="c", reasoning="r", chips=[
            Chip(symbol="BTC_USDT", action="cancel_all", confidence=0.62),
            Chip(symbol="ETH_USDT", action="open_long", confidence=0.0,
                 size_usd=100.0, price=99.0, sl=90.0, tp=110.0),
        ])
        out = _signal(plan)
        self.assertIn("cancel_all", [o["action"] for o in out["orders"]],
                      "撤单的 symbol 与失败的入场腿不同 → 不该被剥")

    def test_global_cancel_dropped_when_all_entry_symbols_failed(self):
        """全局撤单（symbol 为空）：计划里所有要铺单的 symbol 都没通过 → 剥掉。"""
        plan = Plan(cycle_id="c", reasoning="r", chips=[
            Chip(symbol="", action="cancel_all", confidence=0.62),
            Chip(symbol="ETH_USDT", action="open_long", confidence=0.0,
                 size_usd=100.0, price=99.0, sl=90.0, tp=110.0),
        ])
        out = _signal(plan)
        self.assertNotIn("cancel_all", [o["action"] for o in out["orders"]])


class TestLadderSurvivesRejectedRebuild(unittest.TestCase):
    """端到端（离线 paper，走真实 `parse_signal`）：产线那一轮不得撤掉网格。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        tmp = Path(self._td.name)
        self.ex = PaperExchange(env="paper", store_path=tmp / "a.db", feed=_Feed(100.0),
                                config={"initial_capital": 10000, "leverage": 20})
        self.addCleanup(self.ex.store.close)
        self.executor = Executor(self.ex, label_prefix="mtg", root=tmp, bot_id="mg")

    def _ladder(self, n=10):
        return sorted(str(self.ex.place_order(
            {"contract": _SYM, "size": 1, "type": "limit",
             "price": str(99.0 - i * 0.3), "text": "t-mtg"})["id"]) for i in range(n))

    def _ladder_ids(self):
        return sorted(str(o["id"]) for o in self.ex.list_orders(_SYM)
                      if str(o.get("text") or "") == "t-mtg")

    def test_rejected_rebuild_keeps_ladder(self):
        grid = self._ladder()
        # 一个多头持仓（modify_tp_sl 的对象）
        self.ex.place_order({"contract": _SYM, "size": 1, "type": "market",
                             "text": "t-mtg"})
        self.assertEqual(len(self._ladder_ids()), 10)

        payload = _signal(_plan(entry_conf=0.0))
        self.assertNotIn("cancel_all", [o["action"] for o in payload["orders"]])
        report = self.executor.execute_signal(parse_signal(payload))
        self.assertTrue(report.ok, report)
        self.assertEqual(self._ladder_ids(), grid,
                         "入场腿被风控砍掉后，网格必须原样保留"
                         "（修复前：整梯被撤光 ≈18 分钟空窗）")

    def test_accepted_rebuild_still_replaces_ladder(self):
        """对照：入场腿通过 → 旧梯照常被替换（修复不得把正常重铺也拦掉）。"""
        grid = self._ladder()
        payload = _signal(_plan(entry_conf=0.9))
        self.assertIn("cancel_all", [o["action"] for o in payload["orders"]])
        self.assertEqual(payload["replace"], "symbol")
        report = self.executor.execute_signal(parse_signal(payload))
        self.assertTrue(report.ok, report)
        self.assertFalse(set(grid) <= set(self._ladder_ids()),
                         "正常重铺轮应撤掉旧梯（新梯接替）")


if __name__ == "__main__":
    unittest.main()
