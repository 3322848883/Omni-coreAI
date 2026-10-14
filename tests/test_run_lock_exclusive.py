# -*- coding: utf-8 -*-
"""运行锁的父子互斥（N5）。

此前「跑全部」用 `data/bots/_all.run.lock`、「跑单个 bot」用
`data/bots/<bot>/state/run.lock` —— 两把锁**互不相干**，于是 `run` 与
`run --bot X` 能同时驱动同一个 bot（两份进程各读同一 inbox、各自下单）。

修法：`all ⊇ per-bot` —— 跑全部时把每个 bot 的 per-bot 锁一并握住（fail-closed）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.__main__ import acquire_run_locks  # noqa: E402
from omnialpha.watcher import ProjectPaths  # noqa: E402

BOTS = {"a-bot": object(), "b-bot": object()}


class TestRunLockExclusive(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.paths = ProjectPaths(Path(self._td.name))
        self.paths.ensure()

    def tearDown(self):
        self._td.cleanup()

    def _release(self, got):
        lock, held = got
        for h in held:
            h.release()
        lock.release()

    def test_all_mode_acquires_per_bot_locks(self):
        """跑全部必须同时握住每个 bot 的 per-bot 锁。"""
        got = acquire_run_locks(self.paths, BOTS, None)
        self.assertIsNotNone(got)
        _lock, held = got
        self.assertEqual(len(held), len(BOTS))
        self._release(got)

    def test_second_all_mode_is_refused(self):
        first = acquire_run_locks(self.paths, BOTS, None)
        self.assertIsNotNone(first)
        self.assertIsNone(acquire_run_locks(self.paths, BOTS, None))
        self._release(first)

    def test_all_mode_blocks_per_bot_run(self):
        """跑全部时，`run --bot X` 必须被拒（此前可同时驱动）。"""
        all_got = acquire_run_locks(self.paths, BOTS, None)
        self.assertIsNotNone(all_got)
        for bid in BOTS:
            with self.subTest(bot=bid):
                self.assertIsNone(acquire_run_locks(self.paths, BOTS, bid))
        self._release(all_got)

    def test_per_bot_run_blocks_all_mode(self):
        """某个 bot 在跑时，跑全部必须被拒（fail-closed）。"""
        one = acquire_run_locks(self.paths, BOTS, "a-bot")
        self.assertIsNotNone(one)
        self.assertIsNone(acquire_run_locks(self.paths, BOTS, None))
        self._release(one)

    def test_two_different_bots_can_run_concurrently(self):
        """存量行为保留：不同 bot 的 per-bot 运行互不阻塞。"""
        a = acquire_run_locks(self.paths, BOTS, "a-bot")
        b = acquire_run_locks(self.paths, BOTS, "b-bot")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self._release(a)
        self._release(b)

    def test_same_bot_twice_is_refused(self):
        first = acquire_run_locks(self.paths, BOTS, "a-bot")
        self.assertIsNotNone(first)
        self.assertIsNone(acquire_run_locks(self.paths, BOTS, "a-bot"))
        self._release(first)

    def test_release_allows_restart(self):
        got = acquire_run_locks(self.paths, BOTS, None)
        self._release(got)
        again = acquire_run_locks(self.paths, BOTS, None)
        self.assertIsNotNone(again)
        self._release(again)

    def test_partial_failure_rolls_back(self):
        """跑全部时若某个 bot 锁被占 → 整体失败，且**已持有的锁全部释放**。"""
        blocker = acquire_run_locks(self.paths, BOTS, "b-bot")
        self.assertIsNotNone(blocker)
        self.assertIsNone(acquire_run_locks(self.paths, BOTS, None))
        # 回滚验证：释放 blocker 后跑全部应能成功（说明 _all 锁没被泄漏持有）
        self._release(blocker)
        got = acquire_run_locks(self.paths, BOTS, None)
        self.assertIsNotNone(got, "_all 锁被泄漏 —— 回滚没释放")
        self._release(got)


if __name__ == "__main__":
    unittest.main()
