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
from omnialpha.strategist.tools import NATIVE_TOOLS, run_tool  # noqa: E402

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

    # 三个工具源文件都要扫：orderflow_tools 原先的 schema 示例写死 `e.g. BTC_USDT`，
    # 上一版只扫了 tools/tv_tools —— 漏一个文件，那个文件就能重新长回兜底。
    TOOL_SOURCES = (
        "omnialpha/strategist/tools.py",
        "omnialpha/strategist/tv_tools.py",
        "omnialpha/strategist/orderflow_tools.py",
    )

    def test_no_btc_default_in_tool_sources(self):
        for rel in self.TOOL_SOURCES:
            hits = []
            for i, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
                if 'or "BTC_USDT"' in line or "e.g. BTC_USDT" in line:
                    hits.append("%s:%d: %s" % (rel, i, line.strip()[:80]))
            self.assertEqual(hits, [], "仍有硬编码币种兜底：\n" + "\n".join(hits))

    def test_no_symbol_fallback_of_any_coin(self):
        """兜底形态与币名无关：`or "<任何币>_USDT"` / `e.g. <任何币>_USDT` 都不许有。

        只盯 BTC 会漏掉「换成 ETH 写死」这种同形改动。注释行跳过 —— 注释里
        解释「为什么不能写死」是允许的（那正是这条守卫的说明）。
        """
        import re

        pat = re.compile(r'(?:\bor|\be\.g\.)\s*["\']?[A-Z0-9]{2,10}_USDT')
        hits = []
        for rel in self.TOOL_SOURCES:
            for i, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if pat.search(line):
                    hits.append("%s:%d: %s" % (rel, i, line.strip()[:100]))
        self.assertEqual(hits, [], "仍有硬编码币种兜底：\n" + "\n".join(hits))

    def test_run_tool_accepts_universe(self):
        import inspect

        sig = inspect.signature(run_tool)
        self.assertIn("symbols", sig.parameters)

    def test_available_native_tools_accepts_universe(self):
        import inspect

        from omnialpha.strategist.tools import available_native_tools

        sig = inspect.signature(available_native_tools)
        self.assertIn("symbols", sig.parameters)


class _FakeGate:
    """只记录收到了什么 symbol，不真取数 —— 用来断言「拒绝时一个 REST 都没发」。"""

    def __init__(self):
        self.tickers: list = []
        self.order_calls: list = []

    def get_ticker(self, sym):
        self.tickers.append(sym)
        return {"last": "1", "mark_price": "1", "funding_rate": "0"}

    def get_account(self):
        return {"available": "10", "total": "10", "position_mode": "single"}

    def get_positions(self):
        return []

    def list_orders(self, sym=None):
        self.order_calls.append(sym)
        return []

    def list_price_orders(self, sym):
        self.order_calls.append(sym)
        return []


class TestTickerNeverGuesses(unittest.TestCase):
    """`ticker` 空符号曾经拿回**任意币**的数据（`/tickers` 不带 contract 返回全量、
    代码取 `raw[0]`，返回体的 symbol 还是空串 —— 完全静默的错币，A-6/B-6）。"""

    def test_missing_symbol_rejected_without_calling_exchange(self):
        c = _FakeGate()
        out = run_tool(c, "ticker", {}, symbols=MULTI)
        self.assertEqual(out.get("error"), "symbol_required", out)
        self.assertEqual(c.tickers, [], "拒绝时不得发起 /tickers（空 contract 会取回任意币）")

    def test_auto_filled_symbol_is_what_the_exchange_sees(self):
        c = _FakeGate()
        run_tool(c, "ticker", {}, symbols=["ETH_USDT"])
        self.assertEqual(c.tickers, ["ETH_USDT"], "单币自动补必须真的传到取数层")


class TestAccountSymbolsSchema(unittest.TestCase):
    """`account.symbols`：进 schema（enum=宇宙）、与宇宙求交、**归一后**才用。"""

    @staticmethod
    def _account_fn(schemas):
        for t in schemas:
            if (t.get("function") or {}).get("name") == "account":
                return t["function"]
        raise AssertionError("account tool not found")

    def _symbols_prop(self, schemas):
        return self._account_fn(schemas)["parameters"]["properties"]["symbols"]

    def test_schema_declares_symbols_array(self):
        prop = self._symbols_prop(NATIVE_TOOLS)
        self.assertEqual(prop["type"], "array")
        self.assertEqual(prop["items"]["type"], "string")

    def test_enum_injected_from_universe(self):
        from omnialpha.strategist.tools import available_native_tools

        prop = self._symbols_prop(available_native_tools(None, symbols=MULTI))
        self.assertEqual(prop["items"]["enum"], MULTI)

    def test_enum_not_written_into_module_constant(self):
        """多 bot 同进程共用 `NATIVE_TOOLS` —— 注入必须落在副本上（否则串味）。"""
        from omnialpha.strategist.tools import available_native_tools

        available_native_tools(None, symbols=MULTI)
        self.assertNotIn("enum", self._symbols_prop(NATIVE_TOOLS)["items"])

    def test_out_of_universe_rejected_before_any_rest(self):
        c = _FakeGate()
        out = run_tool(c, "account", {"symbols": ["FAKE_USDT"]}, symbols=MULTI)
        self.assertEqual(out.get("error"), "symbol_not_in_universe", out)
        self.assertEqual(c.order_calls, [], "越界时不该发起任何查单 REST")
        self.assertTrue(out.get("universe"), "错误体要带宇宙（模型据此自我纠正）")

    def test_lowercase_is_normalised_not_treated_as_out_of_universe(self):
        """大小写不是「越界」：宇宙里的币名是配置声明的大写形态，`eth_usdt` 是同一个标的。"""
        c = _FakeGate()
        out = run_tool(c, "account", {"symbols": ["eth_usdt"]}, symbols=MULTI)
        self.assertNotIn("error", out, out)
        self.assertIn("ETH_USDT", c.order_calls, "归一后的币必须真的用于查单")

    def test_account_without_symbol_is_still_allowed(self):
        """`account` 是账户级工具：不指定币是合法用法，不该被强制要 symbol。"""
        c = _FakeGate()
        out = run_tool(c, "account", {}, symbols=MULTI)
        self.assertNotIn("error", out, out)


class TestUnifiedPrecheck(unittest.TestCase):
    """T6：**全部**需要 symbol 的工具都走统一前置（不再各自 `or "BTC_USDT"`）。

    此前只有 3 个 smc 工具 + 全部 tv_* 接了统一解析，其余（7 个行情 + 10 个 aux +
    4 个 orderflow）只做 `args.get("symbol") or ""` —— 于是单币漏写不会自动补、
    多币漏写不会拒绝，`BTC`/`BTCUSDT` 这类写法还会静默命中 0 行。
    """

    NEEDS_SYMBOL = (
        "klines", "indicators", "ticker", "orderbook", "contract", "stats", "taker_delta",
        "trades_flow", "liquidations", "market_stats", "tech_analysis",
        "coin_info", "onchain", "social", "sentiment",
        "orderflow_tape", "orderflow_footprint", "orderbook_state", "orderbook_walls",
    )

    def test_multi_universe_rejects_missing_symbol(self):
        for tool in self.NEEDS_SYMBOL:
            out = run_tool(None, tool, {}, symbols=MULTI)
            self.assertEqual(out.get("error"), "symbol_required", (tool, out))
            self.assertEqual(out.get("universe"), MULTI, tool)

    def test_out_of_universe_rejected(self):
        for tool in ("klines", "ticker", "sentiment", "trades_flow", "orderflow_tape"):
            out = run_tool(None, tool, {"symbol": "SOL_USDT"}, symbols=MULTI)
            self.assertEqual(out.get("error"), "symbol_not_in_universe", (tool, out))

    def test_auto_fill_reaches_klines_fetch(self):
        import omnialpha.strategist.tools as T

        seen: list = []
        orig = T.resolve_candles
        T.resolve_candles = lambda client, sym, tf, lim, **kw: (seen.append(sym), _FakeRes())[1]
        try:
            run_tool(None, "klines", {}, symbols=["ETH_USDT"])
        finally:
            T.resolve_candles = orig
        self.assertEqual(seen, ["ETH_USDT"], "单币自动补必须真的传到取数层")

    def test_contract_tool_returns_min_notional(self):
        """`contract` 的描述与 AGENTS.md 规则 #1 都承诺给「最小名义」，实现此前没给。"""
        from types import SimpleNamespace

        class _C:
            def get_contract(self, s):
                return SimpleNamespace(quanto_multiplier=0.0001, order_size_round=1.0,
                                       order_price_round=0.1, leverage_max=100)

            def get_last_price(self, s):
                return 80000.0

        out = run_tool(_C(), "contract", {"symbol": "BTC_USDT"}, symbols=["BTC_USDT"])
        self.assertAlmostEqual(float(out["min_notional_usd"]), 8.0, places=6)

    def test_account_symbols_intersected_with_universe(self):
        out = run_tool(None, "account", {"symbols": ["FAKE_USDT"]}, symbols=MULTI)
        self.assertEqual(out.get("error"), "symbol_not_in_universe", out)

    def test_sentiment_no_longer_falls_back_to_all(self):
        """缺 symbol 不再返回「全表最新 N 行」（那是**任意币**的数据）。"""
        out = run_tool(None, "sentiment", {}, symbols=MULTI)
        self.assertEqual(out.get("error"), "symbol_required", out)


class _SpyLLM:
    """只做一件事：把 `chat_message_full` 收到的 tools 留下来。"""

    def __init__(self):
        self.tools = None

    def chat_message_full(self, messages, tools=None, tool_choice=None):
        self.tools = tools
        return {"content": "no tools this round", "tool_calls": []}


class TestEnumWiringInProductionPath(unittest.TestCase):
    """**接线断言**：生产路径必须把宇宙带进 schema。

    `_bind_symbol_enum` 本身有测试（`TestAccountSymbolsSchema`），但本仓最常见的
    失效形态正是「helper 与测试都写好了、生产调用点没接上」（独立评审一次抓到 5 处，
    而 `available_native_tools(symbols=)` 自己就是第 6 处）。所以这里不扫源码，
    直接跑**生产方法** `_chat_native_tools`、抓它交给 LLM 的 tools。
    """

    @staticmethod
    def _runner(symbols):
        from omnialpha.strategist.loop import PlanRunner, StrategistConfig

        r = PlanRunner.__new__(PlanRunner)
        r.cfg = StrategistConfig(symbols=list(symbols))
        r.llm = _SpyLLM()
        return r

    @staticmethod
    def _symbols_prop(tools):
        for t in tools or []:
            fn = t.get("function") or {}
            if fn.get("name") == "account":
                return fn["parameters"]["properties"]["symbols"]
        raise AssertionError("account 工具不在工具面里（枚举没地方注入）")

    def test_multi_symbol_loop_exposes_universe_in_schema(self):
        r = self._runner(MULTI)
        r._chat_native_tools([{"role": "user", "content": "x"}], max_rounds=0)
        self.assertIsNotNone(r.llm.tools, "生产路径没把 tools 传给 LLM")
        self.assertEqual(self._symbols_prop(r.llm.tools)["items"]["enum"], MULTI,
                         "多币下模型必须直接看到合法标的（否则要靠一次越界往返去猜）")

    def test_single_symbol_loop_gets_unique_enum(self):
        """单币宇宙也给 enum —— 值是**唯一合法解**，不是新增约束。

        I11 例外（工具 schema ≠ prompt 文本）：`account.symbols` 在单币 bot 上
        本来也只能填那一个值，写进 enum 是把隐含约束显式化。
        """
        r = self._runner(["BTC_USDT"])
        r._chat_native_tools([{"role": "user", "content": "x"}], max_rounds=0)
        self.assertEqual(self._symbols_prop(r.llm.tools)["items"]["enum"], ["BTC_USDT"])

    def test_no_universe_leaves_schema_untouched(self):
        r = self._runner([])
        r._chat_native_tools([{"role": "user", "content": "x"}], max_rounds=0)
        self.assertNotIn("enum", self._symbols_prop(r.llm.tools)["items"],
                         "没有宇宙可注入时必须与改动前逐字一致")


if __name__ == "__main__":
    unittest.main()
