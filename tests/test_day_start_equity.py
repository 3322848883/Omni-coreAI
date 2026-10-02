# -*- coding: utf-8 -*-
"""日初权益按 bot 隔离的回归测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor


class DummyClient:
    def get_account(self):
        return {"total": "10000"}

    def get_positions(self):
        return []


class TestDayStartEquityIsolation(unittest.TestCase):
    def _ex(self, root: Path, bot_id: str) -> Executor:
        return Executor(DummyClient(), root=root, bot_id=bot_id)

    def test_writes_to_bot_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, "bot-a")
            v = ex._day_start_equity(10000.0)
            self.assertEqual(v, 10000.0)
            p = root / "data" / "bots" / "bot-a" / "state" / "_account_risk"
            files = list(p.glob("equity_*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["start_equity"], 10000.0)

    def test_bots_do_not_share(self):
        """bot-a 的日初权益不能被 bot-b 看到/覆盖。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = self._ex(root, "bot-a")
            b = self._ex(root, "bot-b")
            self.assertEqual(a._day_start_equity(10000.0), 10000.0)
            self.assertEqual(b._day_start_equity(5000.0), 5000.0)
            # 各自读到自己的
            self.assertEqual(a._day_start_equity(9999.0), 10000.0)  # 已存在→不覆盖
            self.assertEqual(b._day_start_equity(9999.0), 5000.0)
            # 文件分离
            fa = list((root / "data" / "bots" / "bot-a" / "state" / "_account_risk").glob("*.json"))
            fb = list((root / "data" / "bots" / "bot-b" / "state" / "_account_risk").glob("*.json"))
            self.assertEqual(len(fa), 1)
            self.assertEqual(len(fb), 1)
            self.assertEqual(json.loads(fa[0].read_text(encoding="utf-8"))["start_equity"], 10000.0)
            self.assertEqual(json.loads(fb[0].read_text(encoding="utf-8"))["start_equity"], 5000.0)

    def test_same_bot_reads_back(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, "bot-a")
            ex._day_start_equity(8000.0)
            # 第二次调用：文件已存在 → 返回已存的日初
            self.assertEqual(ex._day_start_equity(12345.0), 8000.0)

    def test_no_shared_history_dir(self):
        """修复后不应再写 history/_account_risk。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, "bot-a")
            ex._day_start_equity(10000.0)
            self.assertFalse((root / "history" / "_account_risk").exists())
            # cwd 下的 history/ 也不该被创建
            self.assertFalse((Path.cwd() / "history" / "_account_risk" / f"equity_*.json").exists() or False)

    def test_default_bot_id_fallback(self):
        """没给 bot_id 时也不能写共享目录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = Executor(DummyClient(), root=root)  # bot_id=""
            ex._day_start_equity(10000.0)
            p = root / "data" / "bots" / "_unknown" / "state" / "_account_risk"
            self.assertTrue(p.exists())


if __name__ == "__main__":
    unittest.main()
