"""trade_log_path 的布局 → ledger 必须落在 `<root>/data/bots.db`。

回归：`TradeLogger.log_execution` 曾用 `self.path.parent.parent.parent` 反推 root，
比正确层级少一层，于是 ledger 被写到 `<root>/data/bots/data/bots.db`。
线上实测该错位库里躺着 6061 笔真实成交，而正确的 `data/bots.db` 只有 3 笔。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.ledger import Ledger, default_ledger_path
from omnialpha.tradelog import TradeLogger, trade_log_path


def _exec(log: TradeLogger, bot_id: str = "b1") -> None:
    with mock.patch("omnialpha.monitoring.notify_trade_events"):
        log.log_execution(bot_id, {"plan_cycle": "c1"}, {"ok": True, "steps": []})


class TestLedgerRootInference(unittest.TestCase):
    def test_trade_log_path_layout_yields_repo_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(TradeLogger(trade_log_path(root, "b1"))._ledger_root(), root.resolve())

    def test_non_standard_layout_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertIsNone(TradeLogger(root / "trades.jsonl")._ledger_root())
            self.assertIsNone(TradeLogger(root / "logs" / "trades.jsonl")._ledger_root())

    def test_ledger_lands_at_root_not_nested(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _exec(TradeLogger(trade_log_path(root, "b1")))
            self.assertTrue((root / "data" / "bots.db").exists())
            self.assertFalse((root / "data" / "bots" / "data" / "bots.db").exists())

    def test_trade_readable_from_canonical_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _exec(TradeLogger(trade_log_path(root, "b1")))
            led = Ledger(default_ledger_path(root))
            try:
                self.assertEqual(len(led.recent_trades("b1")), 1)
            finally:
                led.close()

    def test_skipped_layout_writes_no_ledger_anywhere(self):
        """布局不匹配时宁可跳过，也不要用「往上数几层」猜出来的目录写盘。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            _exec(TradeLogger(root / "trades.jsonl"))
            self.assertFalse((root / "data").exists())
            # 旧的 parents[2] 会算到这里
            self.assertFalse((Path(td) / "data").exists())


if __name__ == "__main__":
    unittest.main()
