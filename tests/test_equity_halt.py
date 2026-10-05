"""告警接自动动作：`equity_deviation` → 自动熔断（逐 bot 配置）。

背景（2026-10-05 架构盘点 S18）：`equity_deviation` 告警**只落盘、无人消费**。
本项目调研的结论是「告警不接自动动作 = 没有控制」（Knight Capital 收到 97 封
风控邮件没人处理，亏掉 4.6 亿）—— 告警只有在能改变系统状态时才算一道闸门。

与 `daily_loss_limit_usd` 的分工（这是本闸门存在的理由）：
  - `daily_loss_limit_usd` 是**绝对值**（亏 $20 停手）。小资金账户配不出有意义的
    数：$20 对 $84 权益是 24%，等于「几乎不触发」。
  - 本闸门是**比例**（亏掉日初的 X% 停手），小账户也能表达「亏一成停手」。

三种取值与 `auto_protect` 一致：false（默认，行为与升级前完全一致）/ "dry"（只记
日志）/ true（写 halt.json，当日有效、跨日自动复位）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import merge_account_risk  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.monitoring import TYPE_EQUITY_HALT, AlertStore, read_alerts  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402


class _Client:
    def __init__(self, equity=1000.0):
        self.equity = equity

    def banner(self):
        return ""

    def get_account(self):
        return {"total": str(self.equity), "available": str(self.equity)}

    def get_positions(self):
        return []

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


def _halt_path(root: Path, bot_id: str = "b1") -> Path:
    return root / "data" / "bots" / bot_id / "state" / "halt.json"


class TestThresholdLogic(unittest.TestCase):
    """阈值判定：只拦向下偏离，且必须越过阈值。"""

    def _ex(self, root, risk):
        return Executor(_Client(), symbols_whitelist=["BTC_USDT"], root=root,
                        bot_id="b1", require_sl=False, account_risk=risk)

    def test_disabled_by_default(self):
        """未配 `equity_deviation_halt` → 不启用，一个字节都不写。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt_pct": 10})
            self.assertEqual(ex._check_equity_deviation_halt(1000.0, 500.0), "")
            self.assertFalse(_halt_path(root).exists())

    def test_downside_beyond_threshold_writes_halt(self):
        """向下偏离 12% ≥ 10% → 写熔断标记。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": True,
                                 "equity_deviation_halt_pct": 10})
            reason = ex._check_equity_deviation_halt(1000.0, 880.0)
            self.assertIn("equity_deviation", reason)
            rec = json.loads(_halt_path(root).read_text(encoding="utf-8"))
            self.assertTrue(rec["halt"])
            self.assertIn("equity_deviation", rec["reason"])

    def test_within_threshold_no_halt(self):
        """偏离 5% < 10% → 不动手。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": True,
                                 "equity_deviation_halt_pct": 10})
            self.assertEqual(ex._check_equity_deviation_halt(1000.0, 950.0), "")
            self.assertFalse(_halt_path(root).exists())

    def test_exactly_at_threshold_triggers(self):
        """正好落在阈值上要触发（边界含等号，避免「差一点就放过」）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": True,
                                 "equity_deviation_halt_pct": 10})
            self.assertNotEqual(ex._check_equity_deviation_halt(1000.0, 900.0), "")

    def test_upside_deviation_does_not_halt(self):
        """向上偏离 20% 是盈利，不该停手。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": True,
                                 "equity_deviation_halt_pct": 10})
            self.assertEqual(ex._check_equity_deviation_halt(1000.0, 1200.0), "")
            self.assertFalse(_halt_path(root).exists())

    def test_zero_pct_disables(self):
        """阈值 ≤ 0 视为未启用（与 auto_protect_sl_pct 同口径）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": True,
                                 "equity_deviation_halt_pct": 0})
            self.assertEqual(ex._check_equity_deviation_halt(1000.0, 1.0), "")
            self.assertFalse(_halt_path(root).exists())

    def test_missing_pct_defaults_off(self):
        """开了开关但没给阈值 → 默认 0 → 不启用（宁可不动手，不能乱动手）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": True})
            self.assertEqual(ex._check_equity_deviation_halt(1000.0, 1.0), "")
            self.assertFalse(_halt_path(root).exists())

    def test_dry_mode_records_only(self):
        """dry：只记「本来会熔断」，不写标记。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": "dry",
                                 "equity_deviation_halt_pct": 10})
            self.assertEqual(ex._check_equity_deviation_halt(1000.0, 880.0), "")
            self.assertFalse(_halt_path(root).exists(), "dry 不许写熔断标记")

    def test_dry_mode_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": "DRY",
                                 "equity_deviation_halt_pct": 10})
            self.assertEqual(ex._check_equity_deviation_halt(1000.0, 880.0), "")
            self.assertFalse(_halt_path(root).exists())

    def test_start_zero_is_safe(self):
        """日初权益为 0（拿不到）→ 不做除法、不熔断。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(root, {"equity_deviation_halt": True,
                                 "equity_deviation_halt_pct": 10})
            self.assertEqual(ex._check_equity_deviation_halt(0.0, 0.0), "")
            self.assertFalse(_halt_path(root).exists())


class TestEndToEndThroughSignal(unittest.TestCase):
    """走 `execute_signal` 全路径：日初权益从真实账户读，熔断真的挡住下一笔开仓。"""

    def _ex(self, client, root, **kw):
        return Executor(client, symbols_whitelist=["BTC_USDT"], root=root,
                        bot_id="b1", require_sl=False, **kw)

    def test_halt_written_then_blocks_next_entry(self):
        client = _Client(equity=1000.0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(client, root, account_risk={
                "equity_deviation_halt": True, "equity_deviation_halt_pct": 10})
            ex.execute_signal(_sig())        # 第一轮：建立日初权益 = 1000
            client.equity = 880.0            # 跌 12%
            ex.execute_signal(_sig())        # 触发熔断
            self.assertTrue(_halt_path(root).exists(), "越阈值应落盘熔断标记")
            client.equity = 1000.0           # 权益回升
            rep = ex.execute_signal(_sig())
            self.assertFalse(rep.ok)
            err = rep.results[0].error or ""
            self.assertIn("HALTED", err)
            self.assertIn("auto halt", err)

    def test_close_still_allowed_after_halt(self):
        """熔断只挡开仓；平仓必须放行，否则仓位出不来。"""
        client = _Client(equity=1000.0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(client, root, account_risk={
                "equity_deviation_halt": True, "equity_deviation_halt_pct": 10})
            ex.execute_signal(_sig())
            client.equity = 880.0
            ex.execute_signal(_sig())
            err = None
            try:
                ex._check_account_risk(_sig("close", side="long").intents[0])
            except Exception as e:  # noqa: BLE001
                err = e
            self.assertIsNone(err, "平仓不该被熔断拦")

    def test_disabled_keeps_old_behaviour(self):
        """没开开关时，权益暴跌也不熔断（升级前后行为一致）。"""
        client = _Client(equity=1000.0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = self._ex(client, root, account_risk={})
            ex.execute_signal(_sig())
            client.equity = 400.0
            ex.execute_signal(_sig())
            self.assertFalse(_halt_path(root).exists())


class TestAccountScope(unittest.TestCase):
    """有 `account:` 时熔断标记落在**账户级**路径 —— 多 bot 共账户一次全停。"""

    def test_account_level_halt_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = Executor(_Client(), symbols_whitelist=["BTC_USDT"], root=root,
                          bot_id="b1", require_sl=False, account="gate-main",
                          account_risk={"equity_deviation_halt": True,
                                        "equity_deviation_halt_pct": 10})
            ex._check_equity_deviation_halt(1000.0, 800.0)
            p = root / "data" / "accounts" / "gate-main" / "state" / "halt.json"
            self.assertTrue(p.exists(), "账户级熔断要落在 data/accounts/<name>/state/")
            self.assertFalse(_halt_path(root).exists(), "不该同时写 bot 级路径")

    def test_account_halt_visible_to_sibling_bot(self):
        """同一账户下的另一个 bot 读到同一个标记 → 一起停手。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = Executor(_Client(), symbols_whitelist=["BTC_USDT"], root=root,
                         bot_id="b1", require_sl=False, account="gate-main",
                         account_risk={"equity_deviation_halt": True,
                                       "equity_deviation_halt_pct": 10})
            a._check_equity_deviation_halt(1000.0, 800.0)
            b = Executor(_Client(), symbols_whitelist=["BTC_USDT"], root=root,
                         bot_id="b2", require_sl=False, account="gate-main")
            self.assertIn("equity_deviation", b._read_auto_halt())


class TestAlertRecorded(unittest.TestCase):
    """熔断同时落一条告警，供人工/体检脚本追溯「谁把它停下来的」。"""

    def test_alert_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = AlertStore(root, "b1")
            ex = Executor(_Client(), symbols_whitelist=["BTC_USDT"], root=root,
                          bot_id="b1", require_sl=False, alert_store=store,
                          account_risk={"equity_deviation_halt": True,
                                        "equity_deviation_halt_pct": 10})
            ex._check_equity_deviation_halt(1000.0, 850.0)
            rows = read_alerts(root, "b1", TYPE_EQUITY_HALT)
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["deviation_pct"], -15.0)
            self.assertEqual(rows[0]["threshold_pct"], 10.0)

    def test_no_alert_when_not_triggered(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = AlertStore(root, "b1")
            ex = Executor(_Client(), symbols_whitelist=["BTC_USDT"], root=root,
                          bot_id="b1", require_sl=False, alert_store=store,
                          account_risk={"equity_deviation_halt": True,
                                        "equity_deviation_halt_pct": 10})
            ex._check_equity_deviation_halt(1000.0, 990.0)
            self.assertEqual(read_alerts(root, "b1", TYPE_EQUITY_HALT), [])

    def test_alert_failure_does_not_block_halt(self):
        """告警落盘失败不能妨碍熔断 —— 熔断是保护，告警是记录。"""

        class _BadStore:
            def raise_alert(self, *a, **kw):
                raise RuntimeError("disk full")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ex = Executor(_Client(), symbols_whitelist=["BTC_USDT"], root=root,
                          bot_id="b1", require_sl=False, alert_store=_BadStore(),
                          account_risk={"equity_deviation_halt": True,
                                        "equity_deviation_halt_pct": 10})
            self.assertNotEqual(ex._check_equity_deviation_halt(1000.0, 800.0), "")
            self.assertTrue(_halt_path(root).exists())


class TestMergeTightens(unittest.TestCase):
    """账户级阈值是硬底：bot 可以更早停手，不能更晚。"""

    def test_account_stricter_wins(self):
        merged = merge_account_risk({"equity_deviation_halt_pct": 10.0},
                                    {"equity_deviation_halt_pct": 5.0})
        self.assertEqual(merged["equity_deviation_halt_pct"], 5.0)

    def test_bot_stricter_wins(self):
        merged = merge_account_risk({"equity_deviation_halt_pct": 3.0},
                                    {"equity_deviation_halt_pct": 5.0})
        self.assertEqual(merged["equity_deviation_halt_pct"], 3.0)


if __name__ == "__main__":
    unittest.main()
