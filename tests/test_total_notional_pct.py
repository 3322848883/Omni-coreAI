# -*- coding: utf-8 -*-
"""`max_total_notional_pct`（按比例的总量闸门）回归测试。

背景：原配置只有绝对值 `max_total_notional_usd: 10000`，在实盘（权益 88）相当于
**113× 权益、永不触发** —— 真正 bind 的只剩杠杆物理上限 50×。而加上
`open_*` → `add_*` 映射后仓位会**单调增长**（重复/过期计划每轮加仓），于是
「单调增长 + 无回撤熔断 + 50 倍杠杆」三者叠加。

所以新增**按比例**的 `max_total_notional_pct`：权益 × pct，权益变化时自动跟随
（绝对值做不到这点）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402


class Meta:
    quanto_multiplier = 0.0001


class NotionalClient:
    def __init__(self, size=22, equity=100.0, px=86000.0, equity_error=False):
        self.size = size
        self.equity = equity
        self.px = px
        self.equity_error = equity_error

    def get_positions(self):
        if not self.size:
            return []
        return [{"contract": "BTC_USDT", "size": self.size, "mode": "single",
                 "mark_price": self.px, "entry_price": self.px}]

    def get_account(self):
        if self.equity_error:
            raise RuntimeError("account: down")
        return {"total": self.equity, "available": self.equity}

    def get_contract(self, symbol):
        return Meta()

    def get_last_price(self, symbol):
        return self.px

    def get_position_mode(self):
        return "single"


class TestTotalNotionalPct(unittest.TestCase):
    def _ex(self, client, **ar):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Executor(client, position_policy="manage_only", account_risk=ar,
                        root=Path(td.name), bot_id="t")

    def _intent(self, size_usd):
        return Intent(action="open_long", symbol="BTC_USDT", size_usd=size_usd,
                      meta={"requested_action": "add_long"})

    def test_allows_within_proportional_cap(self):
        # 持仓 22 × 0.0001 × 86000 ≈ 189；权益 100 × 10 = 1000；加 100 → 289 ≤ 1000
        ex = self._ex(NotionalClient(equity=100.0), max_total_notional_pct=10.0)
        ex._check_account_risk(self._intent(100.0))     # 不应抛

    def test_blocks_over_proportional_cap(self):
        ex = self._ex(NotionalClient(equity=100.0), max_total_notional_pct=10.0)
        with self.assertRaises(Exception) as ctx:
            ex._check_account_risk(self._intent(900.0))  # 189 + 900 > 1000
        self.assertIn("MAX_TOTAL_NOTIONAL", str(ctx.exception))
        self.assertIn("权益×10", str(ctx.exception))

    def test_absolute_and_proportional_take_min(self):
        """两个闸门同时配 → 取更小者（绝对值 500 < 比例 1000）。"""
        ex = self._ex(NotionalClient(equity=100.0),
                      max_total_notional_usd=500.0, max_total_notional_pct=10.0)
        with self.assertRaises(Exception) as ctx:
            ex._check_account_risk(self._intent(400.0))  # 189 + 400 = 589 > 500
        self.assertIn("MAX_TOTAL_NOTIONAL", str(ctx.exception))

    def test_proportional_cap_follows_equity(self):
        """权益翻倍 → 上限跟着翻倍（这正是绝对值做不到的）。"""
        it = self._intent(900.0)
        ex100 = self._ex(NotionalClient(equity=100.0), max_total_notional_pct=10.0)
        with self.assertRaises(Exception):
            ex100._check_account_risk(it)                 # 上限 1000 → 1089 超
        ex200 = self._ex(NotionalClient(equity=200.0), max_total_notional_pct=10.0)
        ex200._check_account_risk(self._intent(900.0))     # 上限 2000 → 1089 通过

    def test_unreadable_equity_fails_closed(self):
        """读不到权益 → 拒绝（宁可不下单，也不放过一个算不出来的敞口）。"""
        ex = self._ex(NotionalClient(equity_error=True), max_total_notional_pct=10.0)
        with self.assertRaises(Exception) as ctx:
            ex._check_account_risk(self._intent(100.0))
        self.assertIn("MAX_TOTAL_NOTIONAL", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
