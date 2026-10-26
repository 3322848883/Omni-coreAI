"""取数判据：`skill` / `skill_ref` 不算“取过数据”。

2026-10-26 实测到的问题：
- 第 7 轮：模型只调了 `skill×1` → `data_checked=false`，但**没有触发防偷懒重试**
  （旧判据是 `not self.tool_usage`，把元工具也算进去了）
- 第 8 轮：模型思考原文「I must call a tool first … **to satisfy the requirement**」
  说明它把「必须调工具」当成形式任务，调一次 `klines` 就交差（而那根 bar 数据
  快照里本来就有）

统一口径：`DATA_TOOL_NAMES` 一份常量，`data_checked` 与 `_has_data_call()` 共用。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class HasDataCallTest(unittest.TestCase):
    def _runner(self, tools):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig()
        r.tool_usage = tools
        return r

    def test_skill_only_is_not_data(self):
        r = self._runner([{"tool": "skill", "args": {"name": "brooks_pa"}}])
        self.assertFalse(r._has_data_call(), "只加载技能不算取数（这是本次修复的核心）")

    def test_skill_ref_only_is_not_data(self):
        r = self._runner([{"tool": "skill_ref"}])
        self.assertFalse(r._has_data_call())

    def test_skill_plus_klines_is_data(self):
        r = self._runner([{"tool": "skill"}, {"tool": "klines"}])
        self.assertTrue(r._has_data_call())

    def test_journal_lookup_is_not_data(self):
        """journal_lookup 查的是自己的历史，不是行情 → 不算取数。"""
        r = self._runner([{"tool": "journal_lookup"}])
        self.assertFalse(r._has_data_call())

    def test_known_data_tools(self):
        for name in ("klines", "ticker", "orderbook", "smc_map", "account"):
            self.assertTrue(self._runner([{"tool": name}])._has_data_call(), name)

    def test_empty_usage(self):
        self.assertFalse(self._runner([])._has_data_call())
        self.assertFalse(self._runner([None, {}])._has_data_call())

    def test_summary_and_helper_agree(self):
        """`data_checked` 与 `_has_data_call()` 必须永远一致（同一份常量）。"""
        cases = ([], [{"tool": "skill"}], [{"tool": "klines"}],
                 [{"tool": "skill"}, {"tool": "ticker"}])
        for usage in cases:
            r = self._runner(usage)
            self.assertEqual(r._tool_usage_summary()["data_checked"], r._has_data_call(),
                             "口径不一致：%s" % usage)


class CoverageGuardTest(unittest.TestCase):
    """守卫：所有「按品种取数据」的工具都必须被认成数据工具。

    2026-10-26 手抄清单只写了 10 个，漏掉 `trades_flow` / `taker_delta` / `onchain` /
    `orderflow_*` 等 13 个；而重试提示里明确列着 `trades_flow` —— 模型只调它会被判定
    「没取数」白跑一轮。现在 `DATA_TOOL_NAMES` 从 `tools.SYMBOL_TOOLS` 派生，这条钉住它。
    """

    def test_every_symbol_tool_counts_as_data(self):
        from omnialpha.strategist.loop import DATA_TOOL_NAMES
        from omnialpha.strategist.tools import SYMBOL_TOOLS

        missing = sorted(set(SYMBOL_TOOLS) - set(DATA_TOOL_NAMES))
        self.assertEqual(missing, [], "这些取数工具没被认成数据：%s" % missing)

    def test_aux_and_orderflow_tools_recognized(self):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig()
        for name in ("trades_flow", "taker_delta", "liquidations", "onchain",
                     "orderflow_tape", "orderbook_walls", "sentiment"):
            r.tool_usage = [{"tool": name}]
            self.assertTrue(r._has_data_call(), name)

    def test_account_counts_but_journal_does_not(self):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig()
        r.tool_usage = [{"tool": "account"}]
        self.assertTrue(r._has_data_call(), "account 是取数据（持仓/权益）")
        r.tool_usage = [{"tool": "journal_lookup"}]
        self.assertFalse(r._has_data_call(), "journal 查的是自己的历史，不是行情")


class RetryTest(unittest.TestCase):
    def test_retry_fires_when_only_meta_tool_called(self):
        """核心回归：只调 skill 时必须重试一次；重试里取到数据就返回第二次结果。"""
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(tools={"enabled": True})
        r.tool_usage = []
        calls = []

        def inner(system, user, chart_base64=None):
            calls.append(user)
            if len(calls) == 1:                      # 第一轮：只调 skill
                r.tool_usage.append({"tool": "skill"})
                return "plan-without-data"
            r.tool_usage.append({"tool": "klines"})   # 重试：真取数
            return "plan-with-data"

        r._chat_with_tools_inner = inner
        out = r._chat_with_tools("sys", "user")
        self.assertEqual(out, "plan-with-data")
        self.assertEqual(len(calls), 2, "只调 skill 必须触发一次重试")
        self.assertIn("不算取数", calls[1], "重试提示要说明元工具不计数")

    def test_no_retry_when_data_fetched(self):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(tools={"enabled": True})
        r.tool_usage = []
        calls = []

        def inner(system, user, chart_base64=None):
            calls.append(user)
            r.tool_usage.append({"tool": "klines"})
            return "plan"

        r._chat_with_tools_inner = inner
        self.assertEqual(r._chat_with_tools("s", "u"), "plan")
        self.assertEqual(len(calls), 1, "取过数据就不该重试")


if __name__ == "__main__":
    unittest.main()
