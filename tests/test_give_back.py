# -*- coding: utf-8 -*-
"""浮盈回撤保护（`give_back_pct`）的单元测试。

背景（2026-10-07 核实）：这个字段**此前根本没有实现** ——
`schema.py`（执行侧）不认它、`Intent` 里没有它、`executor.py`/`watcher.py` 零引用。
它只被「要求填写 + 渲染进下一轮上下文给模型自己看」，属于本仓记录过的
「要求了但不消费」那类缺陷。本文件锁住现在真的会平仓。

口径：
  - 武装：峰值浮盈 ≥ 1R（R = |入场 − 本 bot 的 SL| × 张数 × quanto）
    等价于「TP1 之后才武装」——TP1 就设在 1R
  - 平仓：已武装 且 浮盈 ≤ 峰值×(1−pct%) 且 **浮盈 > 0**（保本平仓，绝不亏着平）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor          # noqa: E402
from omnialpha.schema import parse_signal        # noqa: E402

SYM = "BTC_USDT"
ENTRY = 83250.0
SIZE = 40
QUANTO = 0.0001
SL = 83440.0
# R = |83250 − 83440| × 40 × 0.0001 = $0.76
R_USD = abs(ENTRY - SL) * SIZE * QUANTO


class _Meta:
    quanto_multiplier = QUANTO
    min_notional_usd = 1.0


class GBClient:
    """只实现 give_back 用到的客户端方法。mark 可变，用来模拟价格走。"""

    def __init__(self, mark: float, has_position: bool = True, sl: float | None = SL):
        self.mark = mark
        self.has_position = has_position
        self.sl = sl
        self.closed = []

    def get_positions(self):
        if not self.has_position:
            return []
        return [{"contract": SYM, "size": -SIZE, "entry_price": ENTRY, "mode": ""}]

    def get_ticker(self, symbol):
        return {"mark_price": self.mark, "last": self.mark}

    def get_contract(self, symbol):
        return _Meta()

    def list_price_orders(self, symbol):
        if self.sl is None:
            return []
        # reduce-only SL：size > 0 = 平空（保护 short）
        return [{"id": "sl-1", "initial": {"text": "t-brk-sl", "size": SIZE},
                 "trigger": {"price": str(self.sl)}, "status": "open"}]

    def close_position(self, contract, side=None, size=0):
        self.closed.append((contract, side, size))
        return {"id": "close-1", "status": "finished"}


def _ex(client: GBClient, td: str) -> Executor:
    return Executor(client, label_prefix="brk", root=Path(td), bot_id="t")


class TestParse(unittest.TestCase):
    def test_open_short_carries_give_back(self):
        sig = parse_signal({"action": "open_short", "symbol": SYM, "size_usd": 100,
                            "price": 83000, "sl": 83440, "give_back_pct": 40})
        self.assertEqual(sig.intents[0].give_back_pct, 40.0)

    def test_stop_entry_carries_give_back(self):
        sig = parse_signal({"action": "stop_entry_short", "symbol": SYM, "size_usd": 100,
                            "trigger_price": 83000, "tp": 82000, "sl": 83440,
                            "give_back_pct": 30})
        self.assertEqual(sig.intents[0].give_back_pct, 30.0)

    def test_absent_is_none(self):
        sig = parse_signal({"action": "open_long", "symbol": SYM, "size_usd": 100,
                            "price": 83000, "sl": 82000})
        self.assertIsNone(sig.intents[0].give_back_pct)


class TestGiveBack(unittest.TestCase):
    def _run(self, td, client, dry=False):
        return _ex(client, td).check_give_back(SYM, dry=dry)

    def test_not_armed_below_one_r(self):
        """峰值浮盈不到 1R 不武装 —— 否则回撤 40% 就平，锁定的利润不够付手续费。"""
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83174.0)          # cur = 0.304 = 0.4R
            ex = _ex(c, t)
            ex.remember_give_back(SYM, "short", 40)
            out = ex.check_give_back(SYM)
            self.assertFalse(out["armed"])
            self.assertEqual(out["skipped"], "not_armed")
            self.assertEqual(c.closed, [])

    def test_armed_at_one_r_then_no_retrace(self):
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83060.0)          # cur = 0.76 = 1.0R
            ex = _ex(c, t)
            ex.remember_give_back(SYM, "short", 40)
            self.assertTrue(ex.check_give_back(SYM)["armed"])
            c.mark = 83120.0                    # cur = 0.52（回撤 32%，未到 40%）
            out = ex.check_give_back(SYM)
            self.assertEqual(out["skipped"], "no_retrace")
            self.assertEqual(c.closed, [])

    def test_closes_on_retrace(self):
        """峰值 1.0 → 回撤到 0.60（=40%）→ 平仓，且是保本的。"""
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83000.0)          # cur = 1.00
            ex = _ex(c, t)
            ex.remember_give_back(SYM, "short", 40)
            ex.check_give_back(SYM)             # 记下峰值 1.00
            c.mark = 83100.0                    # cur = 0.60，刚好触发
            out = ex.check_give_back(SYM)
            self.assertIn("closed", out)
            self.assertAlmostEqual(out["closed"]["pnl"], 0.60, places=4)
            self.assertEqual(c.closed, [(SYM, "short", 0)])

    def test_never_closes_at_a_loss(self):
        """**关键**：回撤到盈亏平衡以下不归这条路管 —— 那是止损的职责。

        实战含义：这条路径只做「保本平仓」，绝不亏着平。
        """
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83000.0)
            ex = _ex(c, t)
            ex.remember_give_back(SYM, "short", 40)
            ex.check_give_back(SYM)             # 峰值 1.00，已武装
            c.mark = 83300.0                    # cur = −0.20（已转亏）
            out = ex.check_give_back(SYM)
            self.assertEqual(out["skipped"], "not_profitable")
            self.assertEqual(c.closed, [], "绝不能亏着平")

    def test_peak_is_monotonic(self):
        """峰值只上不下 —— 否则回撤一半再反弹会重设峰值，永远触发不了。"""
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83000.0)
            ex = _ex(c, t)
            ex.remember_give_back(SYM, "short", 40)
            p1 = ex.check_give_back(SYM)["peak_pnl"]
            c.mark = 83050.0                    # 浮盈变小
            p2 = ex.check_give_back(SYM)["peak_pnl"]
            self.assertEqual(p1, p2)

    def test_dry_does_not_close(self):
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83000.0)
            ex = _ex(c, t)
            ex.remember_give_back(SYM, "short", 40)
            ex.check_give_back(SYM, dry=True)
            c.mark = 83100.0
            out = ex.check_give_back(SYM, dry=True)
            self.assertEqual(out["skipped"], "dry")
            self.assertEqual(c.closed, [])

    def test_no_owned_sl_skips(self):
        """没有本 bot 的 SL 就没法定义 R → 跳过（交给 ensure_protection）。"""
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83000.0, sl=None)
            ex = _ex(c, t)
            ex.remember_give_back(SYM, "short", 40)
            out = ex.check_give_back(SYM)
            self.assertEqual(out["skipped"], "no_owned_sl")
            self.assertEqual(c.closed, [])

    def test_no_pct_skips(self):
        """计划没给阈值 → 这条保护对它就是不生效。"""
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83000.0)
            out = _ex(c, t).check_give_back(SYM)
            self.assertEqual(out["skipped"], "no_pct")
            self.assertEqual(c.closed, [])

    def test_position_gone_clears_state(self):
        """持仓消失必须清峰值 —— 留着会让下次开仓一上来就「已回撤」。"""
        with tempfile.TemporaryDirectory() as t:
            c = GBClient(mark=83000.0)
            ex = _ex(c, t)
            ex.remember_give_back(SYM, "short", 40)
            ex.check_give_back(SYM)
            p = ex._give_back_path()
            self.assertIn(f"{SYM}|short", json.loads(p.read_text(encoding="utf-8"))["positions"])
            c.has_position = False
            out = ex.check_give_back(SYM)
            self.assertEqual(out["skipped"], "no_position")
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["positions"], {})

    def test_remember_ignores_bad_values(self):
        with tempfile.TemporaryDirectory() as t:
            ex = _ex(GBClient(83000.0), t)
            for bad in (None, 0, -1, 100, 150, "abc"):
                ex.remember_give_back(SYM, "short", bad)
            p = ex._give_back_path()
            # 全坏值 → 一次都没落盘（不写空文件也是对的：没阈值=这条保护不生效）
            got = json.loads(p.read_text(encoding="utf-8"))["positions"] if p.is_file() else {}
            self.assertEqual(got, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
