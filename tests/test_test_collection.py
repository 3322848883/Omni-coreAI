# -*- coding: utf-8 -*-
"""测试收集护栏（N2）—— 「文件在、却从不执行」必须不可能。

2026-10-14 全面检查发现 `tests/` 下 2 个文件是 pytest 风格，`unittest` 收集到
**0 个测试**：`test_skill_force.py`（类未继承 `TestCase`）、
`test_skill_required.py`（`pytest.mark.skipif`）。已改写为 unittest 兼容。

本文件做**静态**护栏（毫秒级，不需要逐个跑测试）：
  ① `tests/test_*.py` 不得 `import pytest`（改用 `unittest.skipUnless` 等原生机制）；
  ② 顶层 `class Test*` 必须（传递地）继承 `unittest.TestCase`；
  ③ 测试文件不得带 UTF-8 BOM。
动态护栏（真的加载并数测试数）在 `scripts/run_tests.py --guard`。

**另一个更强的动态护栏是 pytest 单进程全量跑**：它会把所有测试放进同一个进程，
任何**未恢复的 monkeypatch / 全局状态泄漏**都会立刻变成失败。实测 2026-10-14：
`test_trigger_persist.py` 就地覆盖 `market.fetch_rest_candles` 却从不还原，
导致同进程后续 `test_triggers*.py` 全挂（26 个失败）—— 而逐文件独立进程的
unittest 完全掩盖了它。这正是「双 runner」的价值所在。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TESTS = ROOT / "tests"

#: 允许例外（**必须写明理由**）
ALLOW_PYTEST: dict[str, str] = {}
ALLOW_PLAIN_TEST_CLASS: dict[str, str] = {}


def _test_files() -> list[Path]:
    return sorted(p for p in TESTS.glob("test_*.py"))


def _module_classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}


def _inherits_testcase(node: ast.ClassDef, classes: dict, seen: frozenset = frozenset()) -> bool:
    """**传递地**判断是否继承 TestCase（本地基类如 `BackfillCase` 也要解析）。"""
    for b in node.bases:
        name = ast.unparse(b)
        if "TestCase" in name:
            return True
        base = classes.get(name)
        if base is not None and name not in seen:
            if _inherits_testcase(base, classes, seen | {name}):
                return True
    return False


def _imports_pytest(tree: ast.Module) -> bool:
    """只看**真实 import**，不看 docstring/字符串里出现的 `pytest` 字样。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            if any(a.name == "pytest" or a.name.startswith("pytest.")
                   for a in n.names):
                return True
        elif isinstance(n, ast.ImportFrom):
            if (n.module or "").startswith("pytest"):
                return True
    return False


class TestCollectionGuard(unittest.TestCase):
    def test_no_pytest_import_in_test_files(self):
        bad = []
        for p in _test_files():
            if p.name in ALLOW_PYTEST:
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8-sig"))
            except SyntaxError:
                continue                     # 由 test_test_classes_inherit_testcase 报
            if _imports_pytest(tree):
                bad.append(p.name)
        self.assertEqual(bad, [],
                         "这些测试文件 import 了 pytest（unittest 下会静默不跑）—— "
                         "改用 unittest.skipUnless / TestCase，或登记到 ALLOW_PYTEST")

    def test_test_classes_inherit_testcase(self):
        bad = []
        for p in _test_files():
            if p.name in ALLOW_PLAIN_TEST_CLASS:
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8-sig"))
            except SyntaxError as e:
                bad.append(f"{p.name}: 语法错误 {e}")
                continue
            classes = _module_classes(tree)
            for node in tree.body:
                if not isinstance(node, ast.ClassDef) or not node.name.startswith("Test"):
                    continue
                if not _inherits_testcase(node, classes):
                    bases = [ast.unparse(b) for b in node.bases]
                    bad.append(f"{p.name}::{node.name} 未（传递）继承 TestCase（bases={bases}）")
        self.assertEqual(bad, [],
                         "这些 Test* 类不会被 unittest 收集：\n  " + "\n  ".join(bad))

    def test_no_bom_in_test_files(self):
        """测试文件不得带 UTF-8 BOM（会让 `ast.parse` / 某些工具解析失败）。"""
        bad = [p.name for p in _test_files()
               if p.read_bytes().startswith(b"\xef\xbb\xbf")]
        self.assertEqual(bad, [], "这些测试文件带 BOM，请去掉")

    def test_converted_files_collect_tests(self):
        """N2 修过的两个文件必须真的能被 unittest 收集到测试。"""
        loader = unittest.TestLoader()
        for name in ("tests.test_skill_force", "tests.test_skill_required"):
            with self.subTest(module=name):
                n = loader.loadTestsFromName(name).countTestCases()
                self.assertGreater(n, 0, f"{name} 收集到 0 个测试")

    def test_runner_script_exists(self):
        self.assertTrue((ROOT / "scripts" / "run_tests.py").is_file(),
                        "缺 scripts/run_tests.py（双 runner + 收集护栏）")


if __name__ == "__main__":
    unittest.main()
