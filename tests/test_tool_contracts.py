"""AI 可见的输入契约：工具 schema 必须把「合法取值」讲清楚。

背景 —— `indicators` 的 `names` 参数原先**连 description 都没有**，AI 只能靠猜：
线上 24h 实测 **2,619 次调用因指标名非法被拒**（占 indicators 调用的 **15.4%**），
典型写法是 `rsi`（应为 `rsi14`）、`bb20`（应为 `boll20`）。

这与 `ecf409f`（触发器参数范围没写给 AI）是同一类：**AI 可写但系统会拒的输入，
必须把合法取值摆在它看得到的地方**。schema 是模型唯一必然读到的契约文档，
所以这几条测试守的是「契约没有退化」。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.loop import PlanRunner  # noqa: E402
from omnialpha.strategist.tools import NATIVE_TOOLS  # noqa: E402


def _fn(name: str) -> dict:
    for t in NATIVE_TOOLS:
        if t["function"]["name"] == name:
            return t["function"]
    raise AssertionError("tool not found: %s" % name)


class TestIndicatorsSchema(unittest.TestCase):
    def test_names_param_documents_valid_and_invalid(self):
        props = _fn("indicators")["parameters"]["properties"]
        desc = props["names"].get("description") or ""
        self.assertTrue(desc, "names 必须有 description —— 否则 AI 只能猜")
        for good in ("ema20", "rsi14", "atr14", "boll20"):
            self.assertIn(good, desc, "应给出合法示例 %s" % good)
        for bad in ("rsi", "atr", "bb20", "bbands"):
            self.assertIn(bad, desc, "应点明会被拒的写法 %s" % bad)

    def test_tool_description_mentions_period(self):
        desc = _fn("indicators")["description"]
        self.assertIn("ema20", desc, "工具描述也要给出带周期的示例")


class TestVisionWarnsWhenNoChart(unittest.TestCase):
    def _runner(self, symbols, tfs, timeframe="15m"):
        runner = object.__new__(PlanRunner)
        runner.cfg = SimpleNamespace(
            symbols=list(symbols), timeframe=timeframe, vision_timeframes=list(tfs)
        )
        return runner

    def test_warns_when_no_chart_generated(self):
        """vision 开着却一张图都没生成 → 必须 warning（此前是彻底静默的）。

        静默的后果是「不发图但没人知道」，属于本项目反复出现的失效形态。
        """
        runner = self._runner(["BTC_USDT"], ["15m", "1h"])
        snapshot = {"market": {"BTC_USDT": {}}}          # 没有 candles
        with self.assertLogs("omnialpha.strategist", level="WARNING") as cm:
            out = runner._generate_charts(snapshot)
        self.assertEqual(out, [])
        self.assertTrue(any("一张图都没生成" in m for m in cm.output), cm.output)

    def test_no_warning_when_candles_present(self):
        """有足够 K 线时不该报警（避免把正常情况也报成异常）。"""
        runner = self._runner(["BTC_USDT"], ["15m"])
        rows = [{"t": 1790900000 + i * 900, "o": 84000 + i, "h": 84100 + i,
                 "l": 83900 + i, "c": 84050 + i, "v": 10 + i} for i in range(20)]
        snapshot = {"market": {"BTC_USDT": {"candles": rows}}}
        with self.assertNoLogs("omnialpha.strategist", level="WARNING"):
            out = runner._generate_charts(snapshot)
        self.assertTrue(out, "有 K 线时应能生成图")


class TestMultiSymbolCharts(unittest.TestCase):
    """多币种出图：**每个币都要有图**。

    回归背景：`_generate_charts` 原先写死 `sym = symbols[0]` —— 多币配置下
    第 2..N 个币一张图都没有，模型只能靠数值快照判断它们（「多币配置、单币视野」）。
    这里 monkeypatch 掉渲染（`generate_and_encode`）只看调用，避免测试里真画图。
    """

    def _runner(self, symbols, tfs, timeframe="15m"):
        runner = object.__new__(PlanRunner)
        runner.cfg = SimpleNamespace(
            symbols=list(symbols), timeframe=timeframe, vision_timeframes=list(tfs)
        )
        return runner

    @staticmethod
    def _rows(base):
        return [{"t": 1790900000 + i * 900, "o": base + i, "h": base + 100 + i,
                 "l": base - 100 + i, "c": base + 50 + i, "v": 10 + i} for i in range(20)]

    def _snapshot(self, syms):
        market = {}
        for sym, base in syms.items():
            market[sym] = {"candles": self._rows(base),
                           "tf": {"1h": {"candles": self._rows(base)}}}
        return {"market": market}

    def _capture(self, runner, snapshot):
        from omnialpha.strategist import vision

        seen: list = []
        orig = vision.generate_and_encode
        vision.generate_and_encode = lambda k, symbol="", timeframe="": (
            seen.append((symbol, timeframe)) or "B64")
        try:
            out = runner._generate_charts(snapshot)
        finally:
            vision.generate_and_encode = orig
        return out, seen

    def test_every_symbol_gets_charts(self):
        runner = self._runner(["BTC_USDT", "ETH_USDT", "SOL_USDT"], ["15m", "1h"])
        out, seen = self._capture(runner, self._snapshot(
            {"BTC_USDT": 84000, "ETH_USDT": 2700, "SOL_USDT": 150}))
        self.assertEqual(len(out), 6, "3 币 × 2 周期 = 6 张图")
        for sym in ("BTC_USDT", "ETH_USDT", "SOL_USDT"):
            self.assertIn((sym, "15m"), seen)
            self.assertIn((sym, "1h"), seen)
        # 反断言：不是只有首币有图（旧实现下 ETH/SOL 一张都没有）
        self.assertEqual(len([s for s, _ in seen if s == "ETH_USDT"]), 2)

    def test_other_symbols_survive_missing_data(self):
        """某个币取不到 K 线时，**其余币照常出图**（只是它自己缺图 + 有 warning）。"""
        runner = self._runner(["BTC_USDT", "ETH_USDT"], ["15m"])
        snap = {"market": {"BTC_USDT": {"candles": self._rows(84000)}, "ETH_USDT": {}}}
        with self.assertLogs("omnialpha.strategist", level="WARNING") as cm:
            out, seen = self._capture(runner, snap)
        self.assertEqual(len(out), 1)
        self.assertEqual(seen, [("BTC_USDT", "15m")])
        self.assertTrue(any("ETH_USDT 一张图都没生成" in m for m in cm.output), cm.output)

    def test_single_symbol_unchanged(self):
        """单币配置行为不变（这是回归的另一半：别把单币也搞坏）。"""
        runner = self._runner(["BTC_USDT"], ["15m", "1h"])
        out, seen = self._capture(runner, self._snapshot({"BTC_USDT": 84000}))
        self.assertEqual(sorted(seen), [("BTC_USDT", "15m"), ("BTC_USDT", "1h")])


if __name__ == "__main__":
    unittest.main()
