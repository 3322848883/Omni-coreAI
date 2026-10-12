# -*- coding: utf-8 -*-
"""[盈亏] 上下文段 + exec_result realized_pnl 注入的锁定测试。

背景（2026-10-12 AI 层复盘）：LLM 无盈亏记忆 —— journal exec_result 只含张数，
[近况] 看不到亏损，连亏后模式化合理化继续开仓。修复 = 每轮 sync 交易所盈亏投影 +
build_context 渲染 [盈亏·已实现] 段（近期序列/连亏/当日）+ exec_result 带合计。
HC-08（当日亏损闸门）/ HC-09（连亏减仓）从死条文变活闸门的数据前提。
"""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from omnialpha.memory.context import _exec_tag, _pnl_block, build_context

NOW = int(time.time())


def _fill(pnl: float, ts: int, contract: str = "ETH_USDT") -> dict:
    return {"key": str(ts * 1_000_000), "pnl": pnl, "contract": contract,
            "side": "", "time": ts, "source": "trigger"}


def _write_state(root: Path, bot_id: str, rec: dict) -> None:
    p = root / "data" / "bots" / bot_id / "state" / "exchange_pnl.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


class TestPnlBlock(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bot = "b1"

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_data_returns_empty(self):
        self.assertEqual(_pnl_block(self.root, self.bot, None), "")

    def test_broken_json_returns_empty(self):
        p = self.root / "data" / "bots" / self.bot / "state" / "exchange_pnl.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{broken", encoding="utf-8")
        self.assertEqual(_pnl_block(self.root, self.bot, None), "")

    def test_renders_recent_sequence_in_desc_order(self):
        _write_state(self.root, self.bot, {"totals": {"trades": 3, "wins": 1,
                                                      "pnl": -1.45, "worst": -0.87},
                                           "fills": [
            _fill(-0.87, NOW - 300),
            _fill(+0.26, NOW - 200),
            _fill(-0.84, NOW - 100),
        ]})
        out = _pnl_block(self.root, self.bot, None)
        self.assertIn("[盈亏·已实现]", out)
        self.assertIn("最近平仓(新→旧)", out)
        # 新→旧：-0.84 在最前
        self.assertLess(out.index("-0.84u"), out.index("+0.26u"))
        self.assertLess(out.index("+0.26u"), out.index("-0.87u"))

    def test_loss_streak_counted_from_latest(self):
        # 序列（旧→新）：+0.26, -0.84, -0.87, -0.80 → 连亏 3
        fills = [_fill(+0.26, NOW - 400), _fill(-0.84, NOW - 300),
                 _fill(-0.87, NOW - 200), _fill(-0.80, NOW - 100)]
        _write_state(self.root, self.bot, {"totals": {"trades": 4, "wins": 1},
                                           "fills": fills})
        out = _pnl_block(self.root, self.bot, None)
        self.assertIn("连亏: 3 笔", out)

    def test_no_loss_streak_tag_when_under_two(self):
        fills = [_fill(-0.84, NOW - 200), _fill(+0.26, NOW - 100)]
        _write_state(self.root, self.bot, {"totals": {"trades": 2, "wins": 1},
                                           "fills": fills})
        self.assertNotIn("连亏", _pnl_block(self.root, self.bot, None))

    def test_zero_pnl_fill_dropped(self):
        # 写侧（`_pnl_of`）0 不是平仓 → 渲染侧同样剔除：不进序列、不打断连亏
        fills = [_fill(-1.0, NOW - 300), _fill(0.0, NOW - 200),
                 _fill(-1.0, NOW - 100)]
        _write_state(self.root, self.bot, {"totals": {"trades": 3, "wins": 0},
                                           "fills": fills})
        out = _pnl_block(self.root, self.bot, None)
        self.assertIn("连亏: 2 笔", out)
        self.assertNotIn("+0.00u", out)

    def test_today_pnl_with_equity_pct(self):
        _write_state(self.root, self.bot, {"totals": {"trades": 1, "wins": 0},
                                           "fills": [_fill(-4.85, NOW - 60)]})
        out = _pnl_block(self.root, self.bot, 150.0)
        self.assertIn("当日(UTC)已实现: -4.85u（1 笔）", out)
        self.assertIn("占权益 3.2%", out)          # 4.85/150 = 3.23%

    def test_yesterday_fill_excluded_from_today(self):
        _write_state(self.root, self.bot, {"totals": {"trades": 1, "wins": 0},
                                           "fills": [_fill(-4.85, NOW - 86400 * 2)]})
        out = _pnl_block(self.root, self.bot, 150.0)
        self.assertNotIn("当日(UTC)已实现", out)

    def test_fill_without_time_excluded_from_today(self):
        f = _fill(-4.85, 0)
        f["time"] = ""                              # 损坏时间 → 不进当日，宁缺勿假
        _write_state(self.root, self.bot, {"totals": {"trades": 1, "wins": 0},
                                           "fills": [f]})
        out = _pnl_block(self.root, self.bot, 150.0)
        self.assertNotIn("当日(UTC)已实现", out)
        self.assertIn("最近平仓", out)              # 但序列仍在

    def test_no_equity_omits_pct(self):
        _write_state(self.root, self.bot, {"totals": {"trades": 1, "wins": 0},
                                           "fills": [_fill(-4.85, NOW - 60)]})
        self.assertNotIn("占权益", _pnl_block(self.root, self.bot, None))


class TestExecTag(unittest.TestCase):
    def test_orders_only(self):
        self.assertEqual(_exec_tag({"exec_result": {"orders": 2}}), " [orders=2]")

    def test_no_orders_no_tag(self):
        self.assertEqual(_exec_tag({"exec_result": {"orders": 0}}), "")

    def test_realized_positive_and_negative(self):
        self.assertEqual(
            _exec_tag({"exec_result": {"orders": 1, "realized_pnl": 1.2}}),
            " [orders=1] [realized=+1.20]")
        self.assertEqual(
            _exec_tag({"exec_result": {"orders": 0, "realized_pnl": -0.84}}),
            " [realized=-0.84]")

    def test_bad_realized_ignored(self):
        self.assertEqual(_exec_tag({"exec_result": {"realized_pnl": "abc"}}), "")


class TestBuildContextIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_user_contains_pnl_block(self):
        _write_state(self.root, "b1", {"totals": {"trades": 2, "wins": 0},
                                       "fills": [_fill(-0.84, NOW - 200),
                                                 _fill(-0.87, NOW - 100)]})
        ctx = build_context(self.root, "b1", system_prompt="S",
                            snapshot_text="SNAP", account_equity=150.0)
        self.assertIn("[盈亏·已实现]", ctx["user"])
        self.assertIn("连亏: 2 笔", ctx["user"])
        self.assertIn("占权益", ctx["user"])
        self.assertIn("SNAP", ctx["user"])

    def test_user_without_data_has_no_pnl_block(self):
        ctx = build_context(self.root, "b1", system_prompt="S",
                            snapshot_text="SNAP")
        self.assertNotIn("[盈亏·已实现]", ctx["user"])


if __name__ == "__main__":
    unittest.main()
