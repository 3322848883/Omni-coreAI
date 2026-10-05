"""安全模式：连续失败达阈值时禁止开仓，只允许平/减/改/观望。

参照 nofx 的 `consecutiveAIFailures>=3` → 过滤掉所有 `open_*`、保留 close/hold，
且 AI 恢复后自动退出（`record_success` 清零 streak，所以不需要额外的解除逻辑）。

动因：LLM 异常时仍可能输出看似合理的开仓计划 —— 实测 eth-disc 在薄时段
连续 17 轮挂同一个价位、从不成交。安全模式让「AI 不健康时只减不增」。

阈值 `account_risk.safe_mode_after_failures` **默认 0 = 关闭**，
所以未配置的 bot 行为完全不变（这是本改动的兼容性保证）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402


class _MinClient:
    """只够走到闸门的最小 fake（SAFE_MODE 在 get_contract 之前拦）。"""

    def __init__(self):
        self.placed = []

    def banner(self):
        """`execute_signal` 开头会打印交易所 banner。"""
        return ""

    def get_account(self):
        return {"total": "1000", "available": "1000"}

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


def _root_with_streak(td: str, streak: int) -> Path:
    root = Path(td)
    state = root / "data" / "bots" / "b1" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "health.json").write_text(
        json.dumps({"error_streak": streak}), encoding="utf-8")
    return root


class TestSafeMode(unittest.TestCase):
    def _ex(self, root: Path, **kw):
        return Executor(_MinClient(), symbols_whitelist=["BTC_USDT"],
                        root=root, bot_id="b1", require_sl=False, **kw)

    def test_open_blocked_when_streak_reached(self):
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(_root_with_streak(td, 3),
                          account_risk={"safe_mode_after_failures": 3})
            rep = ex.execute_signal(parse_signal({
                "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
            }))
            self.assertFalse(rep.ok)
            self.assertIn("SAFE_MODE", rep.results[0].error or "")

    def test_stop_entry_blocked_too(self):
        """突破单也是开仓，同样要拦（与 `_open` 同源）。"""
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(_root_with_streak(td, 5),
                          account_risk={"safe_mode_after_failures": 3})
            rep = ex.execute_signal(parse_signal({
                "action": "stop_entry_long", "symbol": "BTC_USDT",
                "size_usd": 100, "trigger_price": 51000,
            }))
            self.assertFalse(rep.ok)
            self.assertIn("SAFE_MODE", rep.results[0].error or "")

    def test_disabled_by_default(self):
        """阈值 0（默认）时完全不拦 —— 未配置的 bot 行为不变。"""
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(_root_with_streak(td, 99), account_risk={})
            rep = ex.execute_signal(parse_signal({
                "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
            }))
            self.assertNotIn("SAFE_MODE", rep.results[0].error or "",
                             "未配置阈值时不该触发安全模式")

    def test_not_triggered_below_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(_root_with_streak(td, 2),
                          account_risk={"safe_mode_after_failures": 3})
            rep = ex.execute_signal(parse_signal({
                "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
            }))
            self.assertNotIn("SAFE_MODE", rep.results[0].error or "")

    def test_missing_health_file_does_not_block(self):
        """没有健康文件（新 bot / 测试环境）不该拦 —— 保持原行为。"""
        with tempfile.TemporaryDirectory() as td:
            ex = self._ex(Path(td), account_risk={"safe_mode_after_failures": 3})
            rep = ex.execute_signal(parse_signal({
                "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
            }))
            self.assertNotIn("SAFE_MODE", rep.results[0].error or "")

    def test_exec_fail_streak_also_triggers(self):
        """run 侧的连续执行失败也要触发（两个计数取大）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "data" / "bots" / "b1" / "state"
            state.mkdir(parents=True, exist_ok=True)
            (state / "health.json").write_text(
                json.dumps({"error_streak": 0}), encoding="utf-8")
            (state / "health.run.json").write_text(
                json.dumps({"exec_fail_streak": 4}), encoding="utf-8")
            ex = self._ex(root, account_risk={"safe_mode_after_failures": 3})
            rep = ex.execute_signal(parse_signal({
                "action": "open_long", "symbol": "BTC_USDT", "size_usd": 100,
            }))
            self.assertIn("SAFE_MODE", rep.results[0].error or "")


if __name__ == "__main__":
    unittest.main()
