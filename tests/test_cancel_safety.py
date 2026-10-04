# -*- coding: utf-8 -*-
"""撤单动作的「持仓感知」安全闸（2026-10-04 事故回归测试）。

**事故链路**（实测，不是推演）：

1. 账户接口抖动 → `loop.py` 降级继续分析，`account["error"]` 有值
2. `snapshot.position_state()` 只看 `positions` 是否为空、**不看 `error`** →
   把「取数失败」读成 `flat`「无持仓、无待成交入场单」，并作为**权威结论**交给 AI
3. 提示词规则 16 要求「持仓状态以 `position_state` 为准，不要从 positions 有无去猜」
4. 提示词规则 14 又强制「孤儿保护单**本轮必须撤销**」
5. `executor._cancel_price_all` **不复核持仓**，直接撤

结果：AI 发出 `cancel_price_all` 撤 `t-wyk-tp`/`t-wyk-sl`，而 BTC_USDT 上
2366 张的真实持仓还在（只因当时没有 `run` 进程消费 inbox 才没出事）。

本文件钉住三层防护：

| 层 | 防护 |
|---|---|
| snapshot | 「取不到」与「取到空」必须分开（`unknown` ≠ `flat`） |
| prompt | 规则 14/16 写明 `unknown` 状态下禁止撤保护单 |
| executor | 撤单动作带持仓感知：保护真实持仓的 tp/sl 跳过；有待成交入场单也跳过；取数失败整拒 |
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import ExecReport, Executor  # noqa: E402
from omnialpha.gate_client import GateApiError  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402
from omnialpha.strategist.prompt import build_system_prompt  # noqa: E402
from omnialpha.strategist.snapshot import position_state  # noqa: E402


class _FakeClient:
    """最小 Gate 形状的假客户端（价格单走 `initial` 包装，普通单走顶层）。"""

    def __init__(self, *, positions=None, price_orders=None, orders=None,
                 fail_positions=False):
        self.positions = list(positions or [])
        self.price_orders = list(price_orders or [])
        self.orders = list(orders or [])
        self.fail_positions = fail_positions
        self.cancelled = []

    def banner(self):
        return "[FAKE]"

    def get_positions(self):
        if self.fail_positions:
            raise GateApiError("positions unavailable")
        return list(self.positions)

    def list_price_orders(self, symbol=""):
        return list(self.price_orders)

    def list_orders(self, symbol=""):
        return list(self.orders)

    def cancel_price_order(self, pid):
        self.cancelled.append(("price", str(pid)))
        return {"id": pid}

    def cancel_order(self, oid):
        self.cancelled.append(("order", str(oid)))
        return {"id": oid}

    def cancel_all_price_orders(self, symbol=None):
        self.cancelled.append(("price_all", symbol))
        return {}

    def cancel_all_orders(self, symbol):
        self.cancelled.append(("all", symbol))
        return {}


def _pos(sym="BTC_USDT", size=2366):
    return {"contract": sym, "size": size, "mode": "single"}


def _tp(oid="p-tp", sym="BTC_USDT", size=-2366, text="t-wyk-tp"):
    """reduce-only 条件保护单（平多 → 负 size）。"""
    return {"id": oid, "initial": {"text": text, "contract": sym,
                                   "is_reduce_only": True, "size": size}}


def _entry_limit(oid="o-brk", sym="BTC_USDT", size=10, text="t-wyk-brk"):
    """未成交的普通限价入场单（非 reduce-only）。"""
    return {"id": oid, "contract": sym, "text": text, "size": size, "left": size}


def _ex(client, **kw):
    kw.setdefault("symbols_whitelist", ["BTC_USDT"])
    kw.setdefault("order_scope", "own")
    kw.setdefault("label_prefix", "wyk")
    return Executor(client, **kw)


def _sig(action="cancel_price_all", symbol="BTC_USDT", label="wyk"):
    return parse_signal({"action": action, "symbol": symbol, "label": label})


# ---------------------------------------------------------------------------
# 第 1 层：position_state 必须区分「取不到」与「取到空」
# ---------------------------------------------------------------------------

class TestPositionState(unittest.TestCase):
    def test_account_error_is_unknown_not_flat(self):
        st, note = position_state({"error": "public get failed", "positions": []})
        self.assertEqual(st, "unknown")
        # note 本身就是 AI 引用的依据 → 必须显式写「不得撤销保护单」
        self.assertIn("不得撤销", note)
        self.assertIn("tp/sl", note)
        self.assertIn("不代表", note)

    def test_truly_empty_is_flat(self):
        st, _ = position_state({"positions": [], "open_orders": []})
        self.assertEqual(st, "flat")

    def test_position_open_unchanged(self):
        st, note = position_state({"positions": [_pos()], "open_orders": []})
        self.assertEqual(st, "position_open")
        self.assertIn("modify_tp_sl", note)

    def test_entry_pending_unchanged(self):
        st, note = position_state({
            "positions": [],
            "open_orders": [{"status": "open"}],
        })
        self.assertEqual(st, "entry_pending")
        self.assertIn("不要发 modify_tp_sl", note)

    def test_missing_positions_key_without_error_is_flat(self):
        # 没有 error 又没给 positions → 按空处理（保持原行为）
        self.assertEqual(position_state({})[0], "flat")


# ---------------------------------------------------------------------------
# 第 2 层：提示词规则必须写明 unknown 状态的处理
# ---------------------------------------------------------------------------

class TestPromptRules(unittest.TestCase):
    def _sys(self):
        return build_system_prompt("人格正文", tools_guide="", skill_catalog="")

    def test_rule_16_mentions_unknown(self):
        s = self._sys()
        self.assertIn("unknown", s)
        self.assertIn("账户取数失败", s)
        self.assertIn("禁止撤销任何 tp/sl 保护单", s)

    def test_rule_14_has_unknown_exception(self):
        s = self._sys()
        self.assertIn("本条不适用", s)
        self.assertIn("无法确认", s)


# ---------------------------------------------------------------------------
# 第 3 层：撤单动作带持仓感知
# ---------------------------------------------------------------------------

class TestCancelPriceAllPositionAware(unittest.TestCase):
    def test_skips_protection_of_live_position(self):
        """事故场景：有真实持仓 + 它的 tp/sl → 一律不撤。"""
        client = _FakeClient(positions=[_pos()], price_orders=[_tp()])
        report = _ex(client).execute_signal(_sig())
        self.assertTrue(report.ok)
        self.assertEqual(client.cancelled, [], "真实持仓的保护单被撤了")
        self.assertEqual(
            report.results[0].detail.get("skipped_live_protection"), ["p-tp"])

    def test_still_cancels_true_orphan(self):
        """真孤儿（无持仓）仍要撤得掉——不能因为加了闸就不干活。"""
        client = _FakeClient(positions=[], price_orders=[_tp()])
        report = _ex(client).execute_signal(_sig())
        self.assertTrue(report.ok)
        self.assertEqual(client.cancelled, [("price", "p-tp")])

    def test_skips_preplaced_protection_when_entry_pending(self):
        """有待成交入场单 → 预挂的保护单不是孤儿（撤了委托一成交就是裸仓）。"""
        client = _FakeClient(positions=[], orders=[_entry_limit()],
                             price_orders=[_tp()])
        report = _ex(client).execute_signal(_sig())
        self.assertEqual(client.cancelled, [], "预挂保护单被撤了")
        self.assertIn("p-tp", report.results[0].detail.get("skipped_live_protection"))

    def test_refuses_when_positions_unavailable(self):
        """持仓取不到 → 整拒（无法区分保护单与孤儿，宁可不动）。"""
        client = _FakeClient(fail_positions=True, price_orders=[_tp()])
        report = _ex(client).execute_signal(_sig())
        self.assertFalse(report.ok)
        self.assertEqual(
            report.results[0].detail.get("refused"), "positions_unavailable")
        self.assertEqual(client.cancelled, [])

    def test_non_own_scope_refuses_when_live_protection(self):
        """整表撤单无法逐单过滤 → 有真实保护单时直接拒绝。"""
        client = _FakeClient(positions=[_pos()], price_orders=[_tp()])
        report = _ex(client, order_scope="all", label_prefix="").execute_signal(_sig())
        self.assertFalse(report.ok)
        self.assertEqual(
            report.results[0].detail.get("refused"), "live_protection_present")
        self.assertEqual(client.cancelled, [])

    def test_short_position_protection_also_skipped(self):
        """空头持仓的保护单（正 size）同样跳过。"""
        client = _FakeClient(
            positions=[_pos(size=-2366)],
            price_orders=[_tp(size=2366, text="t-wyk-tp")])
        report = _ex(client).execute_signal(_sig())
        self.assertEqual(client.cancelled, [])
        self.assertEqual(
            report.results[0].detail.get("skipped_live_protection"), ["p-tp"])

    def test_non_reduce_only_conditional_still_cancelled(self):
        """非 reduce-only 的条件单（入场类）不受闸门影响。"""
        po = {"id": "p-brk", "initial": {"text": "t-wyk-brk",
                                         "contract": "BTC_USDT", "size": 10}}
        client = _FakeClient(positions=[_pos()], price_orders=[po])
        report = _ex(client).execute_signal(_sig())
        self.assertEqual(client.cancelled, [("price", "p-brk")])


class TestCancelAllPositionAware(unittest.TestCase):
    def test_skips_reduce_only_order_of_live_position(self):
        client = _FakeClient(
            positions=[_pos()],
            orders=[{"id": "o-tp", "contract": "BTC_USDT", "text": "t-wyk-tp",
                     "size": -2366, "left": -2366, "is_reduce_only": True}])
        report = _ex(client).execute_signal(_sig("cancel_all"))
        self.assertEqual(client.cancelled, [])
        self.assertIn("o-tp", report.results[0].detail.get("skipped_live_protection"))

    def test_refuses_when_positions_unavailable(self):
        client = _FakeClient(
            fail_positions=True,
            orders=[_entry_limit()])
        report = _ex(client).execute_signal(_sig("cancel_all"))
        self.assertFalse(report.ok)
        self.assertEqual(
            report.results[0].detail.get("refused"), "positions_unavailable")
        self.assertEqual(client.cancelled, [])

    def test_still_cancels_plain_entry_order(self):
        client = _FakeClient(positions=[_pos()], orders=[_entry_limit()])
        report = _ex(client).execute_signal(_sig("cancel_all"))
        self.assertTrue(report.ok)
        self.assertEqual(client.cancelled, [("order", "o-brk")])


class TestReplaceCancelPositionAware(unittest.TestCase):
    """`replace` 的「撤旧单」路径（`_cancel_stale_owned`）同样必须保护真实持仓。

    实测 2026-10-04：r12 那条信号带 `replace`，`cancel_price_all` 已按新闸门跳过保护单，
    但 `_cancel_stale_owned` 照样把 `t-wyk-tp`/`t-wyk-sl` 撤了 —— 同一条语义的
    三条路径（自动清理 / 显式撤单 / replace 撤旧单）必须同源。
    """

    @staticmethod
    def _pre(price_ids=("p-tp",), order_ids=()):
        return {"BTC_USDT": {"prefix": "t-wyk",
                             "price_ids": set(price_ids),
                             "order_ids": set(order_ids)}}

    def test_replace_keeps_live_protection(self):
        client = _FakeClient(positions=[_pos()], price_orders=[_tp()])
        ex = _ex(client)
        report = ExecReport()
        ex._cancel_stale_owned(self._pre(), [], report)
        self.assertEqual(client.cancelled, [], "replace 把真实持仓的保护单撤了")
        self.assertTrue(report.results, "replace_cancel 未触发")
        self.assertIn(("price", "p-tp"),
                      report.results[0].detail.get("skipped_live_protection") or [])

    def test_replace_still_cancels_true_stale_order(self):
        """真陈旧单（无持仓、非保护）仍要被 replace 撤掉。"""
        stale = {"id": "p-old",
                 "initial": {"text": "t-wyk-old", "contract": "BTC_USDT", "size": 10}}
        client = _FakeClient(positions=[], price_orders=[stale])
        ex = _ex(client)
        report = ExecReport()
        ex._cancel_stale_owned(self._pre(price_ids=("p-old",)), [], report)
        self.assertEqual(client.cancelled, [("price", "p-old")])

    def test_replace_keeps_preplaced_protection_when_entry_pending(self):
        """有待成交入场单时，预挂保护单也不能被 replace 撤掉。"""
        client = _FakeClient(positions=[], orders=[_entry_limit()],
                             price_orders=[_tp()])
        ex = _ex(client)
        report = ExecReport()
        ex._cancel_stale_owned(self._pre(), [], report)
        self.assertEqual(client.cancelled, [])

    def test_replace_refuses_when_positions_unavailable(self):
        client = _FakeClient(fail_positions=True, price_orders=[_tp()])
        ex = _ex(client)
        report = ExecReport()
        ex._cancel_stale_owned(self._pre(), [], report)
        self.assertTrue(report.results)
        self.assertFalse(report.results[0].ok)
        self.assertEqual(report.results[0].detail.get("refused"), "positions_unavailable")
        self.assertEqual(client.cancelled, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
