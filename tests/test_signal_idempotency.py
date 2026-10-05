"""执行层信号幂等：同一 (order_id, cycle) 只执行一次。

回归背景（2026-10-05 架构盘点 S3/S8）：文件层的 `.taking` 重命名只能防
「两个进程同时取件」，防不住「同一信号被重复投递」（复制文件、上游重发、
崩溃后重放）—— 执行层**没有任何幂等键**，重复投递就是重复下单。

修法：按 `meta` 的 (order_id, cycle) 建幂等键（缺则退回文件名），
执行成功后记 `executed_signals.jsonl`；重复投递归档到 `archive/duplicate/`。
**失败的不记**（允许重试）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.watcher import (  # noqa: E402
    _already_executed,
    _mark_executed,
    _signal_key,
)


class TestSignalKey(unittest.TestCase):
    def test_uses_order_and_cycle(self):
        key = _signal_key(
            {"meta": {"order_id": "o-1", "plan_cycle": "c-7"}}, "file.json")
        self.assertEqual(key, "o-1|c-7")

    def test_falls_back_to_filename(self):
        self.assertEqual(_signal_key({"meta": {}}, "a-b-c.json"), "a-b-c.json")
        self.assertEqual(_signal_key({}, "x.json"), "x.json")

    def test_cycle_id_alias_accepted(self):
        key = _signal_key({"meta": {"order_id": "o-1", "cycle_id": "c-9"}}, "f")
        self.assertEqual(key, "o-1|c-9")


class TestExecutedLedger(unittest.TestCase):
    class _Paths:
        """只实现 `bot_paths().state` 的最小替身。"""

        def __init__(self, root):
            self.root = Path(root)

        def bot_paths(self, bot_id):
            class _P:
                state = self.root / "state"
            return _P()

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._Paths(td)
            self.assertFalse(_already_executed(paths, "b1", "k1"))
            _mark_executed(paths, "b1", "k1", True)
            self.assertTrue(_already_executed(paths, "b1", "k1"))

    def test_failed_attempt_is_not_recorded(self):
        """失败的不算已执行 —— 否则一次网络抖动就永久屏蔽该信号。"""
        with tempfile.TemporaryDirectory() as td:
            paths = self._Paths(td)
            _mark_executed(paths, "b1", "k2", False)
            self.assertFalse(_already_executed(paths, "b1", "k2"))

    def test_different_keys_independent(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._Paths(td)
            _mark_executed(paths, "b1", "k3", True)
            self.assertTrue(_already_executed(paths, "b1", "k3"))
            self.assertFalse(_already_executed(paths, "b1", "k4"))

    def test_missing_file_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._Paths(td)
            self.assertFalse(_already_executed(paths, "nope", "k"))

    def test_corrupt_line_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._Paths(td)
            _mark_executed(paths, "b1", "k5", True)
            p = Path(td) / "state" / "executed_signals.jsonl"
            with p.open("a", encoding="utf-8") as f:
                f.write("not-json\n")
            self.assertTrue(_already_executed(paths, "b1", "k5"))

    def test_ledger_is_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._Paths(td)
            _mark_executed(paths, "b1", "k6", True)
            p = Path(td) / "state" / "executed_signals.jsonl"
            rec = json.loads(p.read_text(encoding="utf-8").strip())
            self.assertEqual(rec["key"], "k6")
            self.assertTrue(rec["ok"])


if __name__ == "__main__":
    unittest.main()
