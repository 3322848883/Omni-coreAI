"""`run --bot X --allow-disabled` 的选择逻辑。

背景：persona 讨论组需要一个「只执行、不分析」的消费者。组的成员必须保持
`enabled: false`（否则 watchdog 会拉起它们各自的 plan-loop，与讨论组的融合单
在同一账户上互相打架），而 target 账户的 inbox 又要有人消费 —— 原先
`run_forever` 硬过滤 `v.enabled`，于是 `run --bot smc-eth-live` 直接
`SystemExit("no enabled bots to run")`，讨论组产出的信号没人执行。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.watcher import select_bots  # noqa: E402


class _B:
    def __init__(self, enabled):
        self.enabled = enabled


BOTS = {"on": _B(True), "off": _B(False)}


class TestSelectBots(unittest.TestCase):
    def test_default_only_enabled(self):
        self.assertEqual(sorted(select_bots(BOTS)), ["on"])

    def test_only_disabled_is_empty_by_default(self):
        self.assertEqual(select_bots(BOTS, only="off"), {})

    def test_only_enabled_still_works(self):
        self.assertEqual(sorted(select_bots(BOTS, only="on")), ["on"])

    def test_allow_disabled_with_only(self):
        self.assertEqual(sorted(select_bots(BOTS, only="off", allow_disabled=True)), ["off"])

    def test_allow_disabled_without_only_does_not_open_everything(self):
        """没给 only 时 allow_disabled 不能放开全部 disabled bot。"""
        self.assertEqual(sorted(select_bots(BOTS, allow_disabled=True)), ["on"])

    def test_unknown_only_is_empty(self):
        self.assertEqual(select_bots(BOTS, only="nope", allow_disabled=True), {})


if __name__ == "__main__":
    unittest.main()
