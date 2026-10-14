# -*- coding: utf-8 -*-
"""死符号棘轮：不允许「建好但没人调用」的公开符号（F3）。

背景（2026-10-14 全面检查 F3）：此前有 4 个**真正零引用**的公开符号
（`metrics.stats.sharpe_from_equity`、`reconcile.reconcile_bot`、
`skillkit.catalog.catalog_prompt_hint`、`skillkit.loader.ensure_kebab_dirname`）
—— 误导维护者、也让「能力到底接没接线」无法判断。已全部删除。

本测试把「零引用公开符号」变成**可执行的不变量**：全仓扫描，出现新的就红。
（判据：符号名在整个仓库的 `.py` 里只出现 1 次 = 只有定义那一处。）
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 有意保留的零引用符号（**必须写明理由**）。当前为空 —— 保持它为空是最强约束。
ALLOWED: dict[str, str] = {}


def _identifier_counts() -> "collections.Counter[str]":
    """全仓标识符出现次数（一次分词，避免「每个符号扫一遍语料」的 O(n²)）。"""
    import collections
    counts: "collections.Counter[str]" = collections.Counter()
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if "__pycache__" in rel.parts or ".worktrees" in rel.parts:
            continue
        counts.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                                 p.read_text(encoding="utf-8", errors="replace")))
    return counts


def _zero_reference_symbols() -> dict[str, str]:
    counts = _identifier_counts()
    out: dict[str, str] = {}
    for p in sorted((ROOT / "omnialpha").rglob("*.py")):
        if "__pycache__" in p.parts or p.name == "__init__.py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            if node.name.startswith("_"):
                continue
            if counts[node.name] <= 1:          # 只有 `def/class name` 那一处
                out[f"{p.relative_to(ROOT).as_posix()}::{node.name}"] = node.name
    return out


class TestNoDeadSymbols(unittest.TestCase):
    def test_no_new_dead_symbols(self):
        dead = _zero_reference_symbols()
        unexpected = {k: v for k, v in dead.items() if v not in ALLOWED}
        self.assertEqual(
            unexpected, {},
            "发现零引用的公开符号（建好没人调用）—— 请删除，或接入生产并在 "
            "tests/test_no_dead_symbols.py 的 ALLOWED 里写明理由")

    def test_previously_deleted_symbols_stay_deleted(self):
        """F3 删掉的 4 个符号不得复活。"""
        for name in ("sharpe_from_equity", "reconcile_bot",
                     "catalog_prompt_hint", "ensure_kebab_dirname"):
            with self.subTest(symbol=name):
                hits = []
                for p in ROOT.rglob("*.py"):
                    rel = p.relative_to(ROOT)
                    if "__pycache__" in rel.parts or ".worktrees" in rel.parts:
                        continue
                    if re.search(rf"\bdef {name}\b", p.read_text(encoding="utf-8", errors="replace")):
                        hits.append(str(rel))
                self.assertEqual(hits, [], f"{name} 又被加回来了 —— 它是零引用的死代码")

    def test_prompt_single_assembly_path(self):
        """提示词分层：生产路径必须走 `build_system_prompt`（不能自拼 system）。"""
        loop = (ROOT / "omnialpha" / "strategist" / "loop.py").read_text(encoding="utf-8")
        self.assertIn("build_system_prompt", loop)
        # `build_messages` 是 public API（测试/工具用），但生产 loop 不该用它
        self.assertNotIn("build_messages", loop)


if __name__ == "__main__":
    unittest.main()
