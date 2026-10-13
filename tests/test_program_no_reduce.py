# -*- coding: utf-8 -*-
"""程序层不变量：**永不产生「部分出场」**。

背景（2026-10-13 用户拍板「不许程序减仓」）：
程序自主的敞口动作里，唯一会产生「减仓」的是 `ensure_protection` 的**差额补单** ——
`need = size - covered`（覆盖记账口径）被直接当成下单张数（`:2960` `size=need`），
于是 SL 触发时只平掉 need 张 = **部分出场**，既不是全平也不是结构减仓，
没有任何交易理由，纯粹是记账泄漏成了仓位动作。

修正后的不变量：
  程序挂出的 SL，张数**恒等于持仓全量**（触发=全出，属止损）；
  覆盖不全（`0 < covered < size`）时**只告警、不挂单**，交回 AI / 人工。

同时覆盖第二条：`check_give_back` 是「AI 在开仓时预先授权阈值、程序后台执行」的
回调锁盈/保本平仓 —— 它有自己的开关（`account_risk.give_back`），
**不再被 `allow_close`（AI 每轮意图闸门）牵连**。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import BotConfig  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta, GateApiError  # noqa: E402
from omnialpha.watcher import ProjectPaths, _auto_protect_sweep, _give_back_sweep  # noqa: E402


class StubClient:
    """最小 Gate 语义桩：持仓 / 保护单 / 行情 / 挂单 / 平仓。"""

    env = "paper"

    def __init__(self, mark: float = 85000.0):
        self.mark = mark
        self.positions: list[dict] = []
        self.price_orders: list[dict] = []
        self.open_orders: list[dict] = []
        self.placed: list[dict] = []
        self.closed: list[dict] = []
        self._seq = 7000

    def banner(self):
        return "[STUB]"

    # ── account ──
    def get_account(self):
        return {"total": "10000", "available": "10000", "balance": "10000"}

    def get_positions(self):
        return [dict(p) for p in self.positions]

    def set_position(self, size: float):
        self.positions = [] if not size else [
            {"contract": "BTC_USDT", "size": size, "entry_price": 84000}
        ]

    # ── market ──
    def get_ticker(self, symbol=None):
        return {"mark_price": self.mark, "last": self.mark}

    def get_last_price(self, symbol=None):
        return self.mark

    def get_contract(self, symbol=None):
        return ContractMeta(
            name="BTC_USDT", quanto_multiplier=0.0001,
            order_size_round=1.0, order_price_round=0.1, leverage_max=50,
        )

    def get_candles(self, *a, **kw):
        return []

    # ── orders ──
    def list_price_orders(self, symbol=None):
        return [dict(o) for o in self.price_orders]

    def list_orders(self, symbol=None):
        return [dict(o) for o in self.open_orders]

    def get_price_order(self, oid):
        for o in self.price_orders:
            if str(o.get("id")) == str(oid):
                return dict(o)
        raise GateApiError(f"price order not found: {oid}")

    def place_price_order(self, body):
        self._seq += 1
        o = {
            "id": self._seq, "status": "open",
            "initial": dict(body.get("initial") or {}),
            "trigger": dict(body.get("trigger") or {}),
        }
        self.price_orders.append(o)
        self.placed.append(body)
        return dict(o)

    def cancel_price_order(self, pid):
        self.price_orders = [o for o in self.price_orders if str(o.get("id")) != str(pid)]
        return True

    def is_dual_position_mode(self):
        return False

    def get_position_mode(self):
        return "single"

    def close_position(self, contract, side=None, size=0):
        self.closed.append({"contract": contract, "side": side, "size": size})
        return {"id": "close-1", "fill_price": str(self.mark), "pnl": "1.0"}

    # ── 测试辅助：预置一张 owned SL ──
    def add_protector(self, text, size, trigger_price):
        self._seq += 1
        o = {
            "id": self._seq, "status": "open",
            "initial": {"contract": "BTC_USDT", "size": size, "text": text,
                        "is_reduce_only": True, "is_close": True},
            "trigger": {"price": trigger_price, "rule": 2 if size < 0 else 1},
        }
        self.price_orders.append(o)
        return o


def _ex(client, tmp, **kw) -> Executor:
    kw.setdefault("label_prefix", "pt")
    return Executor(client, root=Path(tmp), bot_id="paper-test", **kw)


# ─────────────────────────────────────────────────────────────
# 一、部分覆盖 → 不挂单、只报 partial_coverage
# ─────────────────────────────────────────────────────────────
class TestPartialCoverageNeverPlaces(unittest.TestCase):
    def _armed(self, tmp, size, *, auto_protect=True):
        client = StubClient()
        client.set_position(size)
        return client, _ex(client, tmp, account_risk={
            "auto_protect": auto_protect, "auto_protect_sl_pct": 2.0})

    def test_long_partial_does_not_place(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._armed(td, 10)
            client.add_protector("t-pt-sl", -4, 83000)      # 只覆盖 4 张
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["skipped"], "partial_coverage")
            self.assertEqual(client.placed, [])              # 一个单都没挂
            self.assertEqual(out["position_size"], 10)
            self.assertEqual(out["covered_size"], 4)
            self.assertEqual(out["shortfall"], 6)

    def test_short_partial_does_not_place(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._armed(td, -8)
            client.add_protector("t-pt-sl", 3, 87000)       # 只覆盖 3 张
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["skipped"], "partial_coverage")
            self.assertEqual(client.placed, [])
            self.assertEqual(out["covered_size"], 3)
            self.assertEqual(out["shortfall"], 5)

    def test_partial_reports_preview_fields(self):
        """观察期（含 dry）必须能看到「本来会挂什么」，否则 dry 验证不了任何东西。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._armed(td, 10, auto_protect="dry")
            client.add_protector("t-pt-sl", -4, 83000)
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["skipped"], "partial_coverage")
            self.assertEqual(out["position_side"], "long")
            self.assertIn("mark", out)
            self.assertIn("sl", out)
            self.assertEqual(client.placed, [])


# ─────────────────────────────────────────────────────────────
# 二、零覆盖 → 挂「全量」SL（行为不变）；全覆盖/超覆盖 → 跳过
# ─────────────────────────────────────────────────────────────
class TestPlacementIsAlwaysFullSize(unittest.TestCase):
    def _ex_armed(self, tmp, size):
        client = StubClient()
        client.set_position(size)
        ex = _ex(client, tmp, account_risk={
            "auto_protect": True, "auto_protect_sl_pct": 2.0})
        return client, ex

    def test_naked_long_places_full_size(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._ex_armed(td, 10)
            out = ex.ensure_protection("BTC_USDT")
            self.assertIn("placed", out)
            self.assertEqual(abs(int(client.placed[0]["initial"]["size"])), 10)

    def test_naked_short_places_full_size(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._ex_armed(td, -8)
            out = ex.ensure_protection("BTC_USDT")
            self.assertIn("placed", out)
            self.assertEqual(abs(int(client.placed[0]["initial"]["size"])), 8)

    def test_full_coverage_skips(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._ex_armed(td, 10)
            client.add_protector("t-pt-sl", -10, 83000)
            self.assertEqual(ex.ensure_protection("BTC_USDT")["skipped"], "sl_present")
            self.assertEqual(client.placed, [])

    def test_over_coverage_skips(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._ex_armed(td, 10)
            client.add_protector("t-pt-sl", -25, 83000)
            self.assertEqual(ex.ensure_protection("BTC_USDT")["skipped"], "sl_present")
            self.assertEqual(client.placed, [])

    def test_terminal_sl_does_not_count_as_coverage(self):
        """已终结的 SL 不算覆盖 → 退化成「零覆盖」→ 挂全量（不是差额）。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._ex_armed(td, 10)
            po = client.add_protector("t-pt-sl", -4, 83000)
            po["status"] = "cancelled"
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["covered_size"], 0)
            self.assertIn("placed", out)
            self.assertEqual(abs(int(client.placed[0]["initial"]["size"])), 10)

    def test_invariant_every_placed_sl_is_full_size(self):
        """不变量：任何被挂出的 SL，其 |api_size| 必须等于持仓全量。

        这是防未来回归的护栏 —— 只要有人再引入「按缺口挂单」，这条就红。
        """
        for size in (10, -8, 3, -25):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as td:
                client, ex = self._ex_armed(td, size)
                ex.ensure_protection("BTC_USDT")
                self.assertTrue(client.placed, "应当挂出全量 SL")
                for body in client.placed:
                    self.assertEqual(abs(int(body["initial"]["size"])), abs(size))


# ─────────────────────────────────────────────────────────────
# 三、watcher 接线：partial_coverage 必须告警（且限流）
# ─────────────────────────────────────────────────────────────
def _alerts(td: str, bot_id: str = "paper-test") -> list[dict]:
    p = Path(td) / "data" / "bots" / bot_id / "state" / "alerts.json"
    if not p.is_file():
        return []
    return list(json.loads(p.read_text(encoding="utf-8")).get("alerts") or [])


class TestWatcherPartialCoverageAlert(unittest.TestCase):
    def _bot(self, client, *, auto_protect=True):
        bot = BotConfig(
            bot_id="paper-test", env="paper", symbols=["BTC_USDT"], label_prefix="pt",
            account_risk={"auto_protect": auto_protect, "auto_protect_sl_pct": 2.0},
        )
        bot.create_client = lambda: client
        return bot

    def test_partial_coverage_raises_alert_and_places_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            client = StubClient()
            client.set_position(25)
            client.add_protector("t-pt-sl", -4, 83000)   # 只覆盖 4/25 张
            bot = self._bot(client)
            _auto_protect_sweep(bot, ProjectPaths(root=Path(td)), {})
            self.assertEqual(client.placed, [])
            al = _alerts(td)
            self.assertEqual(len(al), 1, al)
            self.assertEqual(al[0]["type"], "unprotected_position")
            self.assertIn("partial", al[0]["detail"].lower())

    def test_partial_coverage_alert_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as td:
            client = StubClient()
            client.set_position(25)
            client.add_protector("t-pt-sl", -4, 83000)
            bot = self._bot(client)
            alerted: dict = {}
            paths = ProjectPaths(root=Path(td))
            _auto_protect_sweep(bot, paths, alerted)
            _auto_protect_sweep(bot, paths, alerted)     # 同一轮限流窗口内
            self.assertEqual(len(_alerts(td)), 1)

    def test_dry_partial_coverage_does_not_alert(self):
        with tempfile.TemporaryDirectory() as td:
            client = StubClient()
            client.set_position(25)
            client.add_protector("t-pt-sl", -4, 83000)
            bot = self._bot(client, auto_protect="dry")
            with self.assertLogs("omnialpha.watcher", level="INFO") as cm:
                _auto_protect_sweep(bot, ProjectPaths(root=Path(td)), {})
            self.assertEqual(_alerts(td), [])
            self.assertTrue(any("partial" in m.lower() for m in cm.output), cm.output)


# ─────────────────────────────────────────────────────────────
# 四、give_back：单独开关，不受 allow_close 牵连
# ─────────────────────────────────────────────────────────────
class TestGiveBackIndependentSwitch(unittest.TestCase):
    def _armed_position(self, tmp, **kw):
        client = StubClient(mark=86000.0)
        client.set_position(10)
        client.add_protector("t-pt-sl", -10, 83500)      # 1R = 0.5 USD
        ex = _ex(client, tmp, **kw)
        return client, ex

    def test_give_back_closes_even_when_allow_close_false(self):
        """解绑验证：allow_close=false 只禁「AI 每轮的 close 指令」，不压这条路径。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._armed_position(td, allow_close=False,
                                             account_risk={"give_back": True})
            ex.remember_give_back("BTC_USDT", "long", 30)
            ex.check_give_back("BTC_USDT")                # 第 1 次：mark 86000 → 武装
            client.mark = 85200.0                          # 回撤到 1.2（峰值 2.0 的 60%）
            out = ex.check_give_back("BTC_USDT")
            self.assertNotEqual(out.get("skipped"), "close_disabled")
            self.assertEqual(len(client.closed), 1, out)
            self.assertEqual(client.closed[0]["side"], "long")
            self.assertEqual(client.closed[0]["size"], 0)  # 全平，不是部分
            self.assertIn("closed", out["sides"][0])

    def test_manage_positions_false_still_stops_give_back(self):
        """总闸仍然生效：manage_positions=false = 只开仓不管仓。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._armed_position(td, manage_positions=False,
                                             account_risk={"give_back": True})
            ex.remember_give_back("BTC_USDT", "long", 30)
            out = ex.check_give_back("BTC_USDT")
            self.assertEqual(out["skipped"], "manage_positions_off")
            self.assertEqual(client.closed, [])

    def test_sweep_entry_skipped_when_switch_absent(self):
        """sweep 入口的单独开关：没配 account_risk.give_back 就完全不跑。"""
        with tempfile.TemporaryDirectory() as td:
            client = StubClient(mark=85200.0)
            client.set_position(10)
            bot = BotConfig(bot_id="paper-test", env="paper", symbols=["BTC_USDT"],
                            label_prefix="pt", account_risk={})
            bot.create_client = lambda: client
            self.assertEqual(_give_back_sweep(bot, ProjectPaths(root=Path(td)), {}), 0)
            self.assertEqual(client.closed, [])


if __name__ == "__main__":
    unittest.main()
