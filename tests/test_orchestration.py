"""编排层收敛：隐式约束显式化 + broadcast 单实例锁。

回归背景（2026-10-05 架构盘点 S18/S19）：
- `broadcast` 全文**无单实例锁**（对比 run / persona-run 都有）→ 双开重复扇出
- 「persona 组成员必须 enabled: false」只散落在 `watcher.select_bots` 的
  docstring 里 —— 隐式约束靠人记，漏了就双路下单
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.__main__ import _persona_member_enabled_error  # noqa: E402
from omnialpha.pidlock import PidLock  # noqa: E402
from omnialpha.persona.config import PersonaGroup  # noqa: E402


class _Bot:
    def __init__(self, enabled):
        self.enabled = enabled


def _group(members):
    return PersonaGroup(name="g", members=list(members), target_account=members[0],
                        fusion="weighted_vote", on_conflict="hold")


class TestPersonaMemberEnabledGuard(unittest.TestCase):
    def test_enabled_member_is_rejected(self):
        bots = {"a": _Bot(False), "b": _Bot(True)}
        err = _persona_member_enabled_error(bots, [_group(["a", "b"])])
        self.assertIn("b", err)
        self.assertIn("enabled: false", err, "要给出修复指引")

    def test_all_disabled_passes(self):
        bots = {"a": _Bot(False), "b": _Bot(False)}
        self.assertEqual(_persona_member_enabled_error(bots, [_group(["a", "b"])]), "")

    def test_missing_bot_is_not_error(self):
        """成员不在已知 bot 里 → 由 validate_group 报错，这里不越权。"""
        bots = {"a": _Bot(False)}
        self.assertEqual(_persona_member_enabled_error(bots, [_group(["a", "x"])]), "")

    def test_checks_all_groups(self):
        bots = {"a": _Bot(False), "c": _Bot(True)}
        err = _persona_member_enabled_error(
            bots, [_group(["a"]), _group(["c"])])
        self.assertIn("c", err)


class TestBroadcastLock(unittest.TestCase):
    def test_second_instance_is_rejected(self):
        """同一把锁第二次 acquire 必须失败（broadcast 原先无锁）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "broadcast.lock"
            first = PidLock(p).acquire()
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(PidLock(p).acquire(), "双开必须被拒")
            finally:
                first.release()
            # 释放后可再次获取
            again = PidLock(p).acquire()
            self.assertIsNotNone(again)
            again.release()


if __name__ == "__main__":
    unittest.main()
