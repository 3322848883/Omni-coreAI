# -*- coding: utf-8 -*-
"""裸仓自动补保护（`Executor.ensure_protection`）。

背景：`watcher.reconcile_protection()` 只返回警告、生产无人调用；`_orphan_sweep`
只撤孤儿不补。所以「仓位在、SL 不在」在生产里没有任何代码会补回来。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor
from omnialpha.gate_client import ContractMeta, GateApiError


class FakeClient:
    """最小 Gate 语义桩：持仓 / 保护单 / 行情 / 挂单。"""

    env = "paper"

    def __init__(self, mark: float = 85000.0):
        self.mark = mark
        self.positions: list[dict] = []
        self.price_orders: list[dict] = []
        self.open_orders: list[dict] = []
        self.placed: list[dict] = []
        self.fail_place = None
        self._seq = 5000
        self._marks: list[float] = []  # 依次返回，用完后回落到 self.mark

    # ── account ──
    def get_account(self):
        return {"total": "10000", "available": "10000", "balance": "10000"}

    def get_positions(self):
        return list(self.positions)

    def set_position(self, size: float):
        self.positions = [] if not size else [
            {"contract": "BTC_USDT", "size": size, "entry_price": 84000}
        ]

    # ── market ──
    def get_ticker(self, symbol=None):
        mark = self._marks.pop(0) if self._marks else self.mark
        return {"mark_price": mark, "last": mark}

    def get_last_price(self, symbol=None):
        return self.mark

    def get_contract(self, symbol=None):
        return ContractMeta(
            name="BTC_USDT", quanto_multiplier=0.0001,
            order_size_round=1.0, order_price_round=0.1, leverage_max=50,
        )

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
        if self.fail_place:
            raise self.fail_place
        self._seq += 1
        init = dict(body.get("initial") or {})
        o = {
            "id": self._seq, "status": "open",
            "initial": init, "trigger": dict(body.get("trigger") or {}),
        }
        self.price_orders.append(o)
        self.placed.append(body)
        return dict(o)

    def place_protector(self, text, size, trigger_price, reduce_only=True):
        self._seq += 1
        o = {
            "id": self._seq, "status": "open",
            "initial": {"contract": "BTC_USDT", "size": size, "text": text,
                        "is_reduce_only": reduce_only, "is_close": True},
            "trigger": {"price": trigger_price, "rule": 2 if size < 0 else 1},
        }
        self.price_orders.append(o)
        return o

    def place_entry(self, text, size, left=None):
        self._seq += 1
        o = {"id": self._seq, "text": text, "size": size,
             "left": size if left is None else left,
             "is_reduce_only": False, "status": "open"}
        self.open_orders.append(o)
        return o

    def cancel_price_order(self, pid):
        self.price_orders = [o for o in self.price_orders if str(o.get("id")) != str(pid)]
        return True


class TestAutoProtect(unittest.TestCase):
    def _make(self, root: Path, *, auto_protect=True, pct=2.0, mark=85000.0,
              label_prefix="pt"):
        client = FakeClient(mark=mark)
        ex = Executor(
            client,
            label_prefix=label_prefix,
            root=root,
            bot_id="paper-test",
            account_risk={"auto_protect": auto_protect, "auto_protect_sl_pct": pct},
        )
        return client, ex

    # ── 开关 ──────────────────────────────────────────────
    def test_off_by_default(self):
        """默认关：不配 auto_protect 时什么都不做。"""
        with tempfile.TemporaryDirectory() as td:
            client = FakeClient()
            ex = Executor(client, label_prefix="pt", root=Path(td), bot_id="b")
            client.set_position(10)
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["auto_protect"], "off")
            self.assertEqual(client.placed, [])

    def test_pct_zero_is_off(self):
        """auto_protect 开了但 pct<=0 → 视为未启用（不能拿 0% 当止损）。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td), pct=0.0)
            client.set_position(10)
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["auto_protect"], "off")
            self.assertEqual(client.placed, [])

    def test_dry_mode_records_but_places_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td), auto_protect="dry")
            client.set_position(10)
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["auto_protect"], "dry")
            self.assertAlmostEqual(out["sl"], 83300.0)   # 85000 * 0.98
            self.assertAlmostEqual(out["mark"], 85000.0)
            self.assertEqual(out["position_side"], "long")
            self.assertEqual(client.placed, [])

    # ── 跳过条件 ──────────────────────────────────────────
    def test_no_position(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(0)
            self.assertEqual(ex.ensure_protection("BTC_USDT")["skipped"], "no_position")

    def test_sl_present_skips(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            client.place_protector("t-pt-sl", -10, 83000)
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["skipped"], "sl_present")
            self.assertEqual(client.placed, [])

    def test_tp_only_does_not_count_as_protected(self):
        """只有 TP 没有 SL 仍算裸仓 —— reconcile_protection 的 `-sl or -tp` 判据不能沿用。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            client.place_protector("t-pt-tp", -10, 87000)
            out = ex.ensure_protection("BTC_USDT")
            self.assertNotIn("skipped", out)
            self.assertIn("placed", out)

    def test_other_namespace_sl_does_not_count(self):
        """别家的 SL 不算「已受保护」，不能因此漏补自己的。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))  # label_prefix=pt
            client.set_position(10)
            client.place_protector("t-smc-sl", -10, 83000)
            self.assertIn("placed", ex.ensure_protection("BTC_USDT"))

    def test_terminal_sl_does_not_count(self):
        """已终结（cancelled/filled…）的 SL 不算已受保护。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            po = client.place_protector("t-pt-sl", -10, 83000)
            po["status"] = "cancelled"
            self.assertIn("placed", ex.ensure_protection("BTC_USDT"))

    def test_pending_entry_skips(self):
        """有待成交入场单 → 执行器已为它预挂保护单，此刻补会重复。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            client.place_entry("t-pt", 5)
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["skipped"], "pending_entry")
            self.assertEqual(client.placed, [])

    def test_dual_position_ambiguous_skips(self):
        """双向持仓需要先定补哪条腿 —— 兜底逻辑不该猜。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.positions = [
                {"contract": "BTC_USDT", "size": 10, "mode": "dual_long"},
                {"contract": "BTC_USDT", "size": -8, "mode": "dual_short"},
            ]
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["skipped"], "ambiguous_side")
            self.assertEqual(client.placed, [])

    # ── 真挂 ─────────────────────────────────────────────
    def test_naked_long_places_market_sl_below_mark(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            out = ex.ensure_protection("BTC_USDT")
            self.assertIn("placed", out)
            self.assertEqual(len(client.placed), 1)
            body = client.placed[0]
            init, trig = body["initial"], body["trigger"]
            self.assertTrue(init["reduce_only"])            # 只减仓，绝不开新仓
            self.assertEqual(init["text"], "t-pt-sl")       # 本 bot 命名空间 → 可对账
            self.assertEqual(init["size"], -10)             # 卖平多
            self.assertEqual(init["price"], "0")            # 触发即市价，保证出得来
            self.assertEqual(init["tif"], "ioc")
            self.assertEqual(trig["rule"], 2)               # 跌破触发
            self.assertLess(float(trig["price"]), 85000.0)  # 在 mark 下方
            self.assertAlmostEqual(float(trig["price"]), 83300.0)

    def test_naked_short_places_sl_above_mark(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(-8)
            out = ex.ensure_protection("BTC_USDT")
            self.assertIn("placed", out)
            body = client.placed[0]
            self.assertEqual(body["initial"]["size"], 8)    # 买平空
            self.assertEqual(body["trigger"]["rule"], 1)    # 涨破触发
            self.assertGreater(float(body["trigger"]["price"]), 85000.0)
            self.assertAlmostEqual(float(body["trigger"]["price"]), 86700.0)

    def test_sl_size_matches_full_position(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(137)
            ex.ensure_protection("BTC_USDT")
            self.assertEqual(abs(int(client.placed[0]["initial"]["size"])), 137)

    def test_placed_sl_is_seen_on_next_sweep(self):
        """挂完第二轮必须跳过 —— 否则每 300s 补一张，越堆越多。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            self.assertIn("placed", ex.ensure_protection("BTC_USDT"))
            second = ex.ensure_protection("BTC_USDT")
            self.assertEqual(second["skipped"], "sl_present")
            self.assertEqual(len(client.placed), 1)

    # ── 失败路径 ──────────────────────────────────────────
    def test_place_failure_reports_error_without_raising(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            client.fail_place = GateApiError("AUTO_TRIGGER_PRICE_LESS_MARK")
            out = ex.ensure_protection("BTC_USDT")
            self.assertIn("error", out)
            self.assertNotIn("placed", out)

    def test_illegal_side_is_caught_by_precheck(self):
        """mark 在取价与预检之间跳走 → 算出的 SL 落到非法一侧，预检必须拦下。"""
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            # 第 1 次 get_ticker 给 mark=85000（算 SL=83300），
            # 第 2 次（预检内部）给 mark=82000 → 83300 在 mark 上方 = 非法
            client._marks = [85000.0, 82000.0]
            out = ex.ensure_protection("BTC_USDT")
            self.assertIn("error", out)
            self.assertIn("TRIGGER_PRICE_SIDE", out["error"])
            self.assertEqual(client.placed, [])

    def test_no_mark_price_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            client, ex = self._make(Path(td))
            client.set_position(10)
            client.mark = 0.0
            self.assertEqual(ex.ensure_protection("BTC_USDT")["error"], "no mark price")


class TestAutoProtectSweep(unittest.TestCase):
    """watcher 侧接线：`_auto_protect_sweep` 真的会挂、且失败告警有限流。"""

    def _bot(self, **ar):
        from omnialpha.config import BotConfig

        return BotConfig(
            bot_id="paper-test", env="paper", symbols=["BTC_USDT"],
            label_prefix="pt", account_risk=dict(ar),
        )

    def _paths(self, td):
        from omnialpha.watcher import ProjectPaths

        return ProjectPaths(root=Path(td))

    def test_sweep_places_once_then_skips(self):
        from omnialpha.watcher import _auto_protect_sweep

        with tempfile.TemporaryDirectory() as td:
            bot = self._bot(auto_protect=True, auto_protect_sl_pct=2.0)
            client = FakeClient(mark=85000.0)
            client.set_position(10)
            bot.create_client = lambda: client
            alerted: dict = {}
            self.assertEqual(_auto_protect_sweep(bot, self._paths(td), alerted), 1)
            self.assertEqual(len(client.placed), 1)
            # 第二轮已有 SL → 不再挂（否则每 300s 堆一张）
            self.assertEqual(_auto_protect_sweep(bot, self._paths(td), alerted), 0)
            self.assertEqual(len(client.placed), 1)

    def test_sweep_off_by_default_does_nothing(self):
        from omnialpha.watcher import _auto_protect_sweep

        with tempfile.TemporaryDirectory() as td:
            bot = self._bot()  # 没配 account_risk
            client = FakeClient()
            client.set_position(10)
            bot.create_client = lambda: client
            self.assertEqual(_auto_protect_sweep(bot, self._paths(td), {}), 0)
            self.assertEqual(client.placed, [])

    def test_sweep_dry_mode_places_nothing(self):
        from omnialpha.watcher import _auto_protect_sweep

        with tempfile.TemporaryDirectory() as td:
            bot = self._bot(auto_protect="dry", auto_protect_sl_pct=2.0)
            client = FakeClient()
            client.set_position(10)
            bot.create_client = lambda: client
            self.assertEqual(_auto_protect_sweep(bot, self._paths(td), {}), 0)
            self.assertEqual(client.placed, [])

    def test_sweep_failure_alerts_and_rate_limits(self):
        """补保护失败 → 落盘告警；同一窗口内反复失败只告警一次。"""
        from omnialpha.monitoring import read_alerts
        from omnialpha.watcher import _auto_protect_sweep

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bot = self._bot(auto_protect=True, auto_protect_sl_pct=2.0)
            client = FakeClient()
            client.set_position(10)
            client.fail_place = GateApiError("AUTO_TRIGGER_PRICE_LESS_MARK")
            bot.create_client = lambda: client
            alerted: dict = {}
            self.assertEqual(_auto_protect_sweep(bot, self._paths(td), alerted), 0)
            self.assertEqual(_auto_protect_sweep(bot, self._paths(td), alerted), 0)
            alerts = read_alerts(root, "paper-test", "unprotected_position")
            self.assertEqual(len(alerts), 1)
            self.assertIn("AUTO_TRIGGER_PRICE_LESS_MARK", alerts[0]["detail"])


    def test_sweep_dry_logs_skip_reason(self):
        """dry 模式跳过时也要记下原因 —— 否则「没有日志」既可能是「已有保护」，
        也可能是「扫描根本没跑」，dry 观察期就无法验证任何东西。"""
        from omnialpha.watcher import _auto_protect_sweep

        with tempfile.TemporaryDirectory() as td:
            bot = self._bot(auto_protect="dry", auto_protect_sl_pct=2.0)
            client = FakeClient()
            client.set_position(10)
            client.place_protector("t-pt-sl", -10, 83000)  # 已有 SL → 应跳过
            bot.create_client = lambda: client
            with self.assertLogs("omnialpha.watcher", level="INFO") as cm:
                _auto_protect_sweep(bot, self._paths(td), {})
            self.assertTrue(
                any("auto protect[dry]" in m and "sl_present" in m for m in cm.output), cm.output
            )
            self.assertEqual(client.placed, [])


if __name__ == "__main__":
    unittest.main()
