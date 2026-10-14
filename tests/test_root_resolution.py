# -*- coding: utf-8 -*-
"""项目根解析的单一入口（N6）。

背景（2026-10-14 全面检查）：`__main__._root_from_args`、`metrics/cli.py`、
`config._create_paper_client` **各写了一遍**「`--root` > `OMNIALPHA_ROOT` > CWD」。
任何一处漏改就会出现「同一个 `--root`，两条链路看到不同的根」。实测先例：
paper 账户库曾完全不看 `--root`（只认 env/cwd）→ 跨次运行持仓叠加（a98d9dc 修复）。

现在唯一入口是 `paths.resolve_root()`；本文件锁两件事：
  ① 行为（优先级 / expanduser / 空串语义）；
  ② 结构（`OMNIALPHA_ROOT` 在 `omnialpha/` 的**代码行**里只出现在 `paths.py`）。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.paths import resolve_root  # noqa: E402


class TestResolveRootBehavior(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("OMNIALPHA_ROOT")
        os.environ.pop("OMNIALPHA_ROOT", None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("OMNIALPHA_ROOT", None)
        else:
            os.environ["OMNIALPHA_ROOT"] = self._old

    def test_explicit_wins_over_env(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["OMNIALPHA_ROOT"] = str(Path(td) / "env")
            self.assertEqual(resolve_root(Path(td) / "explicit"),
                             (Path(td) / "explicit").resolve())

    def test_env_when_no_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["OMNIALPHA_ROOT"] = td
            self.assertEqual(resolve_root(), Path(td).resolve())
            self.assertEqual(resolve_root(None), Path(td).resolve())

    def test_cwd_when_nothing_set(self):
        self.assertEqual(resolve_root(), Path.cwd().resolve())

    def test_empty_explicit_falls_through_to_env(self):
        """空串视为「未指定」—— `--root ""` 不该把根解析成 cwd 之外的东西。"""
        with tempfile.TemporaryDirectory() as td:
            os.environ["OMNIALPHA_ROOT"] = td
            self.assertEqual(resolve_root(""), Path(td).resolve())
            self.assertEqual(resolve_root("   "), Path(td).resolve())

    def test_blank_env_is_ignored(self):
        os.environ["OMNIALPHA_ROOT"] = "   "
        self.assertEqual(resolve_root(), Path.cwd().resolve())

    def test_result_is_absolute_and_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["OMNIALPHA_ROOT"] = td
            r = resolve_root()
            self.assertTrue(r.is_absolute())
            self.assertEqual(r, r.resolve())

    def test_no_existence_check(self):
        """不校验存在性：调用方可能正在创建根（首次启动建 data/）。"""
        missing = Path(tempfile.gettempdir()) / "omnialpha-definitely-missing-xyz"
        self.assertEqual(resolve_root(missing), missing.resolve())


class TestSingleEntryPoint(unittest.TestCase):
    """结构护栏：`OMNIALPHA_ROOT` 的**读取**只应出现在 `paths.py`。

    只查「读取」（`environ.get(...)` / `environ[...]`），不查：
      · 帮助文本 / docstring 里的字样（`--root` 的 help 会提到它）；
      · **写入方**（`watchdog.py` 用 `env.setdefault(...)` 给子进程注入根 ——
        那是生产者，不是解析者）。
    """

    _READ = re.compile(r"environ(?:\.get)?\s*[\(\[]\s*[\"']OMNIALPHA_ROOT")

    def _read_sites(self) -> list[str]:
        hits = []
        for p in (ROOT / "omnialpha").rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if self._READ.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
        return hits

    def test_env_read_only_in_paths_module(self):
        offenders = [h for h in self._read_sites()
                     if "paths.py:" not in h.replace("\\", "/")]
        self.assertEqual(offenders, [],
                         "OMNIALPHA_ROOT 被 paths.py 之外读取（应统一走 resolve_root）")

    def test_consumers_call_resolve_root(self):
        for rel in ("omnialpha/__main__.py", "omnialpha/metrics/cli.py",
                    "omnialpha/config.py"):
            with self.subTest(file=rel):
                src = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn("resolve_root", src, f"{rel} 未使用单一入口")

    def test_paths_module_exports_resolve_root(self):
        from omnialpha import paths
        self.assertTrue(callable(getattr(paths, "resolve_root", None)))


if __name__ == "__main__":
    unittest.main()
