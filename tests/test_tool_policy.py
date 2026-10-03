"""bot 级工具白/黑名单 + 「显式配空」语义。

两件事都是为了让「哪个 bot / 人格能用哪些工具、指标」变成**可配置、可验证**的，
而不是写死在代码或提示词里 —— 后者靠模型自觉，本项目已反复证明不可靠
（提示词写着「不讨论传统指标」，SMC 照样调 sqzmom 并引用 ATR）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.__main__ import _resolve_list_cfg  # noqa: E402
from omnialpha.strategist.tools import (  # noqa: E402
    META_TOOL_NAMES,
    NATIVE_TOOLS,
    TOOL_NAMES,
    filter_tool_schemas,
    validate_tool_policy,
)


def _names(schemas):
    return [(t.get("function") or {}).get("name") for t in schemas]


class TestFilterToolSchemas(unittest.TestCase):
    def test_no_policy_keeps_everything(self):
        self.assertEqual(_names(filter_tool_schemas(NATIVE_TOOLS)),
                         _names(NATIVE_TOOLS))

    def test_allow_is_whitelist(self):
        out = _names(filter_tool_schemas(
            NATIVE_TOOLS, allow=["smc_map", "smc_events", "klines"]))
        self.assertEqual(sorted(out),
                         sorted(["smc_map", "smc_events", "klines", *META_TOOL_NAMES]))

    def test_deny_removes(self):
        out = _names(filter_tool_schemas(NATIVE_TOOLS, deny=["sqzmom", "indicators"]))
        self.assertNotIn("sqzmom", out)
        self.assertNotIn("indicators", out)
        self.assertIn("klines", out)

    def test_deny_wins_over_allow(self):
        out = _names(filter_tool_schemas(
            NATIVE_TOOLS, allow=["smc_map", "smc_events", "klines"], deny=["klines"]))
        self.assertNotIn("klines", out)
        self.assertIn("smc_map", out)

    def test_glob_patterns(self):
        """按族关掉（如 tv_* / smc_*）—— 新加同族工具自动被挡住。"""
        out = _names(filter_tool_schemas(NATIVE_TOOLS, deny=["tv_*", "smc_*"]))
        self.assertFalse([n for n in out if n.startswith("tv_")])
        self.assertFalse([n for n in out if n.startswith("smc_")])
        self.assertIn("klines", out)

    def test_meta_tools_always_kept(self):
        """skill / skill_ref 被白名单挡掉等于把 SkillKit 弄坏。"""
        out = _names(filter_tool_schemas(NATIVE_TOOLS, allow=["klines"]))
        for m in META_TOOL_NAMES:
            self.assertIn(m, out)


class TestValidateToolPolicy(unittest.TestCase):
    def test_unknown_allow_name_raises(self):
        with self.assertRaises(ValueError):
            validate_tool_policy(allow=["indcators"], known=TOOL_NAMES)

    def test_unknown_deny_name_raises(self):
        """拼错的 deny 会**静默地什么都不禁** —— 必须启动就炸。"""
        with self.assertRaises(ValueError):
            validate_tool_policy(deny=["sqzmo"], known=TOOL_NAMES)

    def test_glob_matching_nothing_raises(self):
        with self.assertRaises(ValueError):
            validate_tool_policy(deny=["zzz_*"], known=TOOL_NAMES)

    def test_valid_policy_passes(self):
        validate_tool_policy(allow=["smc_map", "smc_events", "klines"],
                             deny=["tv_*"], known=TOOL_NAMES)

    def test_empty_policy_passes(self):
        validate_tool_policy(allow=None, deny=[], known=TOOL_NAMES)


class TestListCfgEmptyVsMissing(unittest.TestCase):
    def test_missing_uses_default(self):
        self.assertEqual(_resolve_list_cfg({}, "indicators", ["ema20", "atr14"]),
                         ["ema20", "atr14"])

    def test_null_uses_default(self):
        self.assertEqual(_resolve_list_cfg({"indicators": None}, "indicators", ["ema20"]),
                         ["ema20"])

    def test_explicit_empty_disables(self):
        """`indicators: []` 必须真的关掉 —— 不能用 `or` 兜底成默认。"""
        self.assertEqual(_resolve_list_cfg({"indicators": []}, "indicators", ["ema20"]), [])

    def test_explicit_value_used(self):
        self.assertEqual(_resolve_list_cfg({"indicators": ["ema20"]}, "indicators", ["x"]),
                         ["ema20"])


class TestIndicatorWantedSemantics(unittest.TestCase):
    """`wanted=None`（没配→默认）与 `wanted=[]`（显式关掉→一个都不挂）必须可区分。

    这个语义在仓库里被 `or` 兜底错了**三层**（`__main__` 配置层 / `snapshot` 快照层 /
    `indicators` 函数层），实测后果：SMC 配了 `indicators: []`，reasoning 里仍然
    引用「EMA20=84712、ATR=22、RSI 43」—— 配置看起来生效，实际没生效。
    """

    def test_none_uses_default(self):
        from omnialpha.strategist.indicators import DEFAULT_INDICATORS, _resolve_wanted
        self.assertEqual(_resolve_wanted(None), DEFAULT_INDICATORS)

    def test_empty_means_nothing(self):
        from omnialpha.strategist.indicators import _resolve_wanted
        self.assertEqual(_resolve_wanted([]), [])

    def test_explicit_list_used(self):
        from omnialpha.strategist.indicators import _resolve_wanted
        self.assertEqual(_resolve_wanted(["ema20"]), ["ema20"])

    def test_attach_with_empty_list_adds_no_columns(self):
        from omnialpha.strategist.indicators import attach_indicators
        rows = [{"t": i, "o": 1.0, "h": 2.0, "l": 0.0, "c": 1.0 + i * 0.1, "v": 1.0}
                for i in range(40)]
        out = attach_indicators(rows, [])
        for name in ("ema20", "ema50", "atr14", "rsi14"):
            self.assertNotIn(name, out[0], "%s 不该被挂上" % name)

    def test_attach_with_none_adds_default_columns(self):
        from omnialpha.strategist.indicators import attach_indicators
        rows = [{"t": i, "o": 1.0, "h": 2.0, "l": 0.0, "c": 1.0 + i * 0.1, "v": 1.0}
                for i in range(40)]
        out = attach_indicators(rows, None)
        self.assertIn("ema20", out[0])

    def test_latest_with_empty_list_returns_empty(self):
        from omnialpha.strategist.indicators import latest_indicators
        rows = [{"t": i, "c": 1.0} for i in range(5)]
        self.assertEqual(latest_indicators(rows, []), {})


class TestRunToolEnforcement(unittest.TestCase):
    """只过滤 schema **不够** —— 调用点必须也拦。

    实测（SMC 白名单只放 3 个工具）：跑 5 轮，**第 3 轮**模型凭空报出了
    `sqzmom` / `tv_rsi_yata` —— 不在 schema 里，但 `run_tool` 当时只查全局
    `TOOL_NAMES`，于是调用成功、指标数据照样进来了。schema 管「看不到」，
    调用点管「调不到」，缺一不可。
    """

    def test_allowed_passes(self):
        from omnialpha.strategist.tools import _tool_allowed
        self.assertTrue(_tool_allowed("smc_map", allow=["smc_map", "klines"]))
        self.assertTrue(_tool_allowed("klines", allow=["smc_map", "klines"]))
        self.assertTrue(_tool_allowed("skill", allow=["smc_map"]))  # 元工具

    def test_out_of_allow_blocked(self):
        from omnialpha.strategist.tools import _tool_allowed
        self.assertFalse(_tool_allowed("sqzmom", allow=["smc_map"]))
        self.assertFalse(_tool_allowed("tv_rsi_yata", allow=["smc_map"]))

    def test_denied_blocked(self):
        from omnialpha.strategist.tools import _tool_allowed
        self.assertFalse(_tool_allowed("tv_rsi_yata", deny=["tv_*"]))
        self.assertFalse(_tool_allowed("sqzmom", deny=["sqzmom"]))

    def test_no_policy_allows_everything(self):
        from omnialpha.strategist.tools import _tool_allowed
        self.assertTrue(_tool_allowed("sqzmom"))
        self.assertTrue(_tool_allowed("sqzmom", allow=None, deny=[]))

    def test_run_tool_refuses_out_of_policy(self):
        """即使模型凭空报出 sqzmom，run_tool 也必须拒绝（client 用不到就传 None）。"""
        from omnialpha.strategist.tools import run_tool
        out = run_tool(None, "sqzmom", {"symbol": "BTC_USDT"},
                       allow=["smc_map", "smc_events", "klines"])
        self.assertIsInstance(out, dict)
        self.assertIn("error", out)
        self.assertIn("not enabled", out["error"])


if __name__ == "__main__":
    unittest.main()
