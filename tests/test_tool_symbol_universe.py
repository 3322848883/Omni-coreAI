# -*- coding: utf-8 -*-
"""工具 symbol 的解析规则：**币种是参数，不是硬编码常量**。

回归背景（2026-10-09 实测）：6 个工具各自 `or "BTC_USDT"` 兜底
（`tools.py` 3 处 + `tv_tools.py` 3 处）—— 换币种/多币种时模型只要漏写一次 symbol，
工具就**静默返回另一个币的数据**，不报错、不留痕，而模型的整个判断建立在错误标的上。

现在判据集中在 `omnialpha/strategist/symbols.py`（一处定义、多处执行）：
显式给则校验宇宙；没给且宇宙唯一则自动补；多币/未配置/越界一律**拒绝**。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.strategist.symbols import (  # noqa: E402
    AMBIGUOUS_MULTI,
    AUTO_SINGLE,
    EXPLICIT,
    NO_UNIVERSE,
    OUT_OF_UNIVERSE,
    UNIVERSE_EMPTY,
    resolve_symbol_arg,
    symbol_error_payload,
)
from omnialpha.strategist.tools import run_tool  # noqa: E402

MULTI = ["BTC_USDT", "ETH_USDT"]


class TestResolveRules(unittest.TestCase):
    def test_explicit_in_universe(self):
        self.assertEqual(resolve_symbol_arg({"symbol": "eth_usdt"}, MULTI), ("ETH_USDT", EXPLICIT))

    def test_sym_alias_accepted(self):
        self.assertEqual(resolve_symbol_arg({"sym": "ETH_USDT"}, MULTI), ("ETH_USDT", EXPLICIT))

    def test_explicit_out_of_universe(self):
        self.assertEqual(resolve_symbol_arg({"symbol": "SOL_USDT"}, MULTI),
                         ("", OUT_OF_UNIVERSE))

    def test_single_universe_autofills(self):
        self.assertEqual(resolve_symbol_arg({}, ["ETH_USDT"]), ("ETH_USDT", AUTO_SINGLE))

    def test_multi_universe_requires_explicit(self):
        self.assertEqual(resolve_symbol_arg({}, MULTI), ("", AMBIGUOUS_MULTI))

    def test_empty_universe_is_not_guessed(self):
        self.assertEqual(resolve_symbol_arg({}, []), ("", UNIVERSE_EMPTY))

    def test_no_universe_and_no_symbol_is_not_guessed(self):
        """旧调用方（没传宇宙）也不许再静默用 BTC —— 这是本次要根除的形态。"""
        self.assertEqual(resolve_symbol_arg({}, None), ("", NO_UNIVERSE))

    def test_no_universe_with_explicit_symbol_still_works(self):
        """兼容：旧脚本显式给了 symbol，没有宇宙也放行（只是不做白名单校验）。"""
        self.assertEqual(resolve_symbol_arg({"symbol": "BTC_USDT"}, None), ("BTC_USDT", EXPLICIT))

    def test_error_payload_is_self_correcting(self):
        p = symbol_error_payload(AMBIGUOUS_MULTI, MULTI, tool="smc_map")
        self.assertEqual(p["error"], "symbol_required")
        self.assertEqual(p["universe"], MULTI)
        self.assertTrue(p["hint"])
        p2 = symbol_error_payload(OUT_OF_UNIVERSE, MULTI, tool="sqzmom")
        self.assertEqual(p2["error"], "symbol_not_in_universe")


class _FakeRes:
    rows: list = []
    source = "fake"


class TestToolsHonourUniverse(unittest.TestCase):
    """三个曾经硬编码 BTC 的 smc 工具 + TV 工具入口。"""

    def test_multi_universe_rejects_missing_symbol(self):
        for tool in ("smc_map", "smc_events", "sqzmom"):
            out = run_tool(None, tool, {}, symbols=MULTI)
            self.assertEqual(out.get("error"), "symbol_required", (tool, out))
            self.assertEqual(out.get("universe"), MULTI, tool)

    def test_out_of_universe_rejected(self):
        out = run_tool(None, "sqzmom", {"symbol": "SOL_USDT"}, symbols=MULTI)
        self.assertEqual(out.get("error"), "symbol_not_in_universe", out)

    def test_no_universe_no_symbol_rejected(self):
        """不传宇宙且漏写 symbol → 拒绝，而不是悄悄分析 BTC。"""
        out = run_tool(None, "sqzmom", {})
        self.assertEqual(out.get("error"), "symbol_required", out)

    def test_tv_entry_rejects_missing_symbol(self):
        out = run_tool(None, "tv_wyckoff", {}, symbols=MULTI)
        self.assertEqual(out.get("error"), "symbol_required", out)

    def test_single_universe_autofills_into_fetch(self):
        """自动补全必须真的传到取数层（只测返回值会漏掉「补了但没用」）。"""
        import omnialpha.strategist.tools as T
        import omnialpha.strategist.tv_tools as TV

        seen: list = []

        def fake_resolve(client, sym, tf, lim, **kw):
            seen.append(sym)
            return _FakeRes()

        for mod, tool in ((T, "smc_map"), (TV, "tv_wyckoff")):
            seen.clear()
            orig = mod.resolve_candles
            mod.resolve_candles = fake_resolve
            try:
                run_tool(None, tool, {}, symbols=["ETH_USDT"])
            finally:
                mod.resolve_candles = orig
            self.assertEqual(seen, ["ETH_USDT"], "%s 没有把自动补的 symbol 传下去" % tool)


class TestNoHardcodedSymbolLeft(unittest.TestCase):
    """反断言：源码里不得再有硬编码币种兜底（防回归）。"""

    def test_no_btc_default_in_tool_sources(self):
        for rel in ("omnialpha/strategist/tools.py", "omnialpha/strategist/tv_tools.py"):
            hits = []
            for i, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
                if 'or "BTC_USDT"' in line or "e.g. BTC_USDT" in line:
                    hits.append("%s:%d: %s" % (rel, i, line.strip()[:80]))
            self.assertEqual(hits, [], "仍有硬编码币种兜底：\n" + "\n".join(hits))

    def test_run_tool_accepts_universe(self):
        import inspect

        sig = inspect.signature(run_tool)
        self.assertIn("symbols", sig.parameters)


if __name__ == "__main__":
    unittest.main()
