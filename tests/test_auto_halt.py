"""halt 自动触发：日亏超限落盘熔断标记，跨日自动复位。

背景（2026-10-05 架构盘点 S12）：`halt` 只能人工改 yaml —— 出了事要有人
在场才停得下来。而「日亏超限」原先只在**有开仓意图**时才拦：AI 改说
close/modify 就绕过去了，下一轮再提开仓又要重算一遍。

修法：日亏超限时落盘一个**自动熔断标记**（含当日日期），此后所有开仓类动作
一律被 halt 挡住；标记跨日自动失效，所以不需要任何定时任务来解除。
人工 `halt: true` 仍然独立生效（两者逻辑或）。
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402


class _Client:
    def __init__(self, equity=1000.0, positions=None):
        self.equity = equity
        self._positions = positions or []

    def banner(self):
        return ""

    def get_account(self):
        return {"total": str(self.equity), "available": str(self.equity)}

    def get_positions(self):
        return self._positions

    def list_orders(self, contract=None):
        return []

    def list_price_orders(self, contract=None):
        return []

    def get_contract(self, symbol):
        class _M:
            quanto_multiplier = 0.0001
            order_size_min = 1
            price_tick = 0.1
            min_notional_usd = 1.0
        return _M()

    def get_last_price(self, symbol):
        return 50000.0

    def get_ticker(self, symbol):
        return {"mark_price": 50000.0}


def _sig(action="open_long", **kw):
    base = {"action": action, "symbol": "BTC_USDT", "size_usd": 50}
    base.update(kw)
    return parse_signal(base)


class TestAutoHalt(unittest.TestCase):
    def _ex(self, client, root, **kw):
        return Executor(client, symbols_whitelist=["BTC_USDT"], root=root,
                        bot_id="b1", require_sl=False, **kw)

    def test_daily_loss_writes_halt_and_raises(self):
        client = _Client(equity=1000.0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(client, root, account_risk={"daily_loss_limit_usd": 20})
            # 第一轮：建立日初权益
            ex.execute_signal(_sig())
            # 亏损 100 > 20
            client.equity = 900.0
            rep = ex.execute_signal(_sig())
            self.assertFalse(rep.ok)
            self.assertIn("DAILY_LOSS_LIMIT", rep.results[0].error or "")
            # 标记落盘
            p = root / "data" / "bots" / "b1" / "state" / "halt.json"
            self.assertTrue(p.exists(), "日亏超限应落盘熔断标记")
            self.assertTrue(json.loads(p.read_text(encoding="utf-8"))["halt"])

    def test_halt_blocks_open_even_when_loss_recovers(self):
        """熔断置位后，即使权益回升也不放开开仓（要等跨日）。"""
        client = _Client(equity=1000.0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(client, root, account_risk={"daily_loss_limit_usd": 20})
            ex.execute_signal(_sig())
            client.equity = 900.0
            ex.execute_signal(_sig())          # 触发熔断
            client.equity = 1050.0             # 权益回升
            rep = ex.execute_signal(_sig())
            self.assertFalse(rep.ok)
            self.assertIn("HALTED", rep.results[0].error or "")
            self.assertIn("auto halt", rep.results[0].error or "")

    def test_halt_resets_next_day(self):
        """跨日自动复位（不需要定时任务）。"""
        client = _Client(equity=1000.0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / "data" / "bots" / "b1" / "state" / "halt.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
            p.write_text(json.dumps({"halt": True, "reason": "daily_loss_limit",
                                     "day": yesterday}), encoding="utf-8")
            ex = self._ex(client, root)
            self.assertEqual(ex._read_auto_halt(), "", "昨天的标记应已失效")

    def test_close_not_blocked_by_halt(self):
        """熔断只挡开仓，平仓类动作必须放行（否则仓位出不来）。"""
        client = _Client(equity=1000.0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / "data" / "bots" / "b1" / "state" / "halt.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            p.write_text(json.dumps({"halt": True, "reason": "daily_loss_limit",
                                     "day": today}), encoding="utf-8")
            ex = self._ex(client, root)
            err = None
            try:
                ex._check_account_risk(_sig("close", side="long").intents[0])
            except Exception as e:  # noqa: BLE001
                err = e
            self.assertIsNone(err, "平仓不该被熔断拦")

    def test_manual_halt_still_works(self):
        client = _Client(equity=1000.0)
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(_Client(equity=1000.0), Path(td),
                          account_risk={"halt": True})
            rep = ex.execute_signal(_sig())
            self.assertFalse(rep.ok)
            self.assertIn("HALTED", rep.results[0].error or "")
            self.assertIn("account_risk.halt=true", rep.results[0].error or "")


if __name__ == "__main__":
    unittest.main()
