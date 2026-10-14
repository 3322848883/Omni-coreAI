# -*- coding: utf-8 -*-
"""吞异常的留痕与计数（N3）。

全仓 180+ 处 `except Exception:` 后直接 `pass` / `return 空`。多数是刻意的
「增强不阻断主流程」，问题在**静默** —— 「本该有数据却拿到空」事后无从发现。
`observability.swallow()` 的约定：吞可以，但必须留痕 + 计数，且**绝不改控制流**。
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha import observability  # noqa: E402


class TestSwallowAccounting(unittest.TestCase):
    def setUp(self):
        observability.reset_counts()

    def tearDown(self):
        observability.reset_counts()

    def test_counts_increment(self):
        observability.swallow("a.b", ValueError("x"))
        observability.swallow("a.b", "boom")
        observability.swallow("c.d", RuntimeError("y"))
        c = observability.counts()
        self.assertEqual(c["a.b"], 2)
        self.assertEqual(c["c.d"], 1)
        self.assertEqual(observability.total(), 3)

    def test_counts_sorted_desc(self):
        for _ in range(3):
            observability.swallow("hot", "e")
        observability.swallow("cold", "e")
        self.assertEqual(list(observability.counts()), ["hot", "cold"])

    def test_never_raises_even_with_garbage(self):
        """可观测性自身绝不冒泡 —— 否则「吞」变成「炸」。"""
        for bad in (None, "", 123, object()):
            observability.swallow(bad, None)
        observability.swallow("x", object(), level="not-a-level")  # type: ignore[arg-type]
        self.assertGreaterEqual(observability.total(), 5)

    def test_invalid_level_falls_back_to_debug(self):
        with self.assertLogs("omnialpha.observability", level="DEBUG") as cm:
            observability.swallow("x.y", "e", level=99999)  # type: ignore[arg-type]
        self.assertTrue(any("x.y" in m for m in cm.output))

    def test_warning_level_is_honoured(self):
        with self.assertLogs("omnialpha.observability", level="WARNING") as cm:
            observability.swallow("crit.path", "e", level=logging.WARNING)
        self.assertTrue(any("crit.path" in m for m in cm.output))

    def test_debug_level_not_emitted_at_warning(self):
        logger = logging.getLogger("omnialpha.observability")
        old = logger.level
        logger.setLevel(logging.WARNING)
        try:
            with self.assertRaises(AssertionError):     # assertLogs 会因「无日志」失败
                with self.assertLogs("omnialpha.observability", level="WARNING"):
                    observability.swallow("quiet.path", "e")   # 默认 DEBUG
        finally:
            logger.setLevel(old)
        # 但**计数**照记（留痕级别可低，计数不能少）
        self.assertEqual(observability.counts()["quiet.path"], 1)

    def test_reset_clears(self):
        observability.swallow("a", "e")
        observability.reset_counts()
        self.assertEqual(observability.counts(), {})


class TestCriticalPathUsesSwallow(unittest.TestCase):
    """账户/持仓读取失败必须留痕（这一路径的静默曾导致误判）。"""

    def setUp(self):
        observability.reset_counts()

    def tearDown(self):
        observability.reset_counts()

    def test_symbol_positions_swallows_with_warning(self):
        from omnialpha.executor import Executor

        class Boom:
            def get_positions(self):
                raise RuntimeError("account unreachable")

        ex = Executor(Boom(), label_prefix="t")
        with self.assertLogs("omnialpha.observability", level="WARNING") as cm:
            out = ex._symbol_positions("BTC_USDT")
        self.assertEqual(out, [])                       # 控制流不变
        self.assertTrue(any("_symbol_positions" in m for m in cm.output))
        self.assertEqual(observability.counts()["executor._symbol_positions"], 1)


class TestAuditRatchet(unittest.TestCase):
    """审计 B12 的棘轮：裸吞异常不得比基线更多。"""

    def test_no_growth_over_baseline(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "audit", ROOT / "scripts" / "audit_program_layers.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        now = mod._bare_swallow_counts()
        grew = {k: (v, mod._BARE_SWALLOW_BASELINE.get(k, 0))
                for k, v in now.items()
                if v > mod._BARE_SWALLOW_BASELINE.get(k, 0)}
        self.assertEqual(grew, {},
                         "新增了裸吞异常 —— 请改用 observability.swallow() 留痕")


if __name__ == "__main__":
    unittest.main()
