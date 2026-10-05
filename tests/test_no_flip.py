"""禁止同轮反手：持多时不能直接 `open_short`（必须先平）。

参照 nofx 的 `tradeThrottleReason`（「已有仓位禁止反手开」）。反手在同一轮里
会先开反向仓、再平旧仓（或反之），保证金占用翻倍且顺序不确定 —— 拆成两轮更安全。

开关 `account_risk.no_flip` **默认开启**（更安全的一侧）；显式写 false 才关闭。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402


class _Client:
    def __init__(self, pos_size=4):
        self.pos_size = pos_size

    def banner(self):
        return ""

    def get_positions(self):
        if self.pos_size == 0:
            return []
        return [{"contract": "BTC_USDT", "size": self.pos_size,
                 "entry_price": 50000.0}]

    def get_contract(self, symbol):
        class _M:
            quanto_multiplier = 0.0001
            order_size_min = 1
            price_tick = 0.1
            order_price_round = 0.1
            min_notional_usd = 1.0
        return _M()

    def get_last_price(self, symbol):
        return 50000.0

    def get_ticker(self, symbol):
        return {"mark_price": 50000.0}


def _sig(action, **kw):
    base = {"action": action, "symbol": "BTC_USDT", "size_usd": 50}
    base.update(kw)
    return parse_signal(base)


class TestNoFlip(unittest.TestCase):
    def _ex(self, client, **kw):
        return Executor(client, symbols_whitelist=["BTC_USDT"],
                        root=Path(tempfile.mkdtemp()), bot_id="b1",
                        require_sl=False, **kw)

    def test_flip_blocked_when_long(self):
        """持多时开空 → 拒。"""
        ex = self._ex(_Client(pos_size=4), account_risk={})
        err = None
        try:
            ex._check_no_flip(_sig("open_short").intents[0])
        except Exception as e:  # noqa: BLE001
            err = e
        self.assertIsNotNone(err)
        self.assertIn("NO_FLIP", str(err))

    def test_same_side_allowed(self):
        """持多时开多 → 放行（那是加仓，由 `_map_open_to_add` 处理）。"""
        ex = self._ex(_Client(pos_size=4), account_risk={})
        ex._check_no_flip(_sig("open_long").intents[0])  # 不该抛

    def test_flat_allows_both(self):
        ex = self._ex(_Client(pos_size=0), account_risk={})
        ex._check_no_flip(_sig("open_short").intents[0])
        ex._check_no_flip(_sig("open_long").intents[0])

    def test_can_be_disabled(self):
        ex = self._ex(_Client(pos_size=4), account_risk={"no_flip": False})
        ex._check_no_flip(_sig("open_short").intents[0])  # 不该抛

    def test_stop_entry_also_checked(self):
        ex = self._ex(_Client(pos_size=4), account_risk={})
        err = None
        try:
            ex._check_no_flip(_sig("stop_entry_short", trigger_price=49000).intents[0])
        except Exception as e:  # noqa: BLE001
            err = e
        self.assertIsNotNone(err, "突破进场也要拦反手")

    def test_close_not_affected(self):
        ex = self._ex(_Client(pos_size=4), account_risk={})
        ex._check_no_flip(_sig("close", side="long").intents[0])  # 不该抛


if __name__ == "__main__":
    unittest.main()
