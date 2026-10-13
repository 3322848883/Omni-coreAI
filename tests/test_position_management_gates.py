# -*- coding: utf-8 -*-
"""持仓管理闸门：`manage_positions` / `allow_close` / `allow_reduce`。

背景（2026-10-13 账户归属事故）：引擎没有「自开持仓」概念 —— `order_scope`
只按 label 前缀过滤**订单**，`get_positions()` 是账户级全量；模型按人格
§54「有仓必须给仓位管理结论」去管**用户手动开的仓**。实测证据（BROOKS
账户 my_trades）：用户 07:18 app 手动多 25 张 → bot 07:32 减 15 → 08:16
挂 SL 2517.42 → 08:57 止损剩余 10 张。

根治靠账户隔离（运营铁律：bot 账户禁放手动单）。本文件测的是**第二层
防线**：把「平仓 / 减仓 / 改保护」做成可选配置 ——

  manage_positions: false → 快照与 account 工具不再注入持仓/保护单；
      executor 拒收一切持仓管理动作（close/close_all/flatten/reduce*/
      modify_tp_sl/trail），watcher 的三条 sweep（auto_protect /
      peak_trail / give_back）同样熄火 → 「只开仓、不管仓」模式。
  allow_close: false  → 只禁平仓（close/close_all/flatten），减仓与改保护放行。
      ⚠️ **不作用于 give_back**（AI 开仓时预先授权、程序 60s 扫描执行的回调锁盈
      平仓）—— 那条由 `account_risk.give_back` 单独控制（2026-10-13 解绑）。
  allow_reduce: false → 只禁减仓（reduce*），平仓与改保护放行。

注意 expand_signal 的别名映射：reduce/reduce_long/reduce_short 都落成
action="close"（meta.requested_action 记原动作），flatten 落成
close_all —— 闸门必须按 **requested_action** 判，否则 allow_reduce=false
会被 action="close" 漏掉。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import BotConfig, load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta, GateApiError  # noqa: E402
from omnialpha.schema import Intent, SignalFile  # noqa: E402
from omnialpha.strategist.snapshot import collect_snapshot  # noqa: E402
from omnialpha.strategist.tools import run_tool  # noqa: E402


class RecorderClient:
    """记录每一次交易所调用。

    闸门测试的核心断言：被拒的动作**一个交易所调用都不该发生** ——
    `calls` 必须保持全空（`banner()` 除外，它在 `_execute_intent` 第一行）。
    """

    env = "paper"

    def __init__(self):
        self.calls: list = []
        self.positions: list[dict] = []
        self.price_orders: list[dict] = []
        self.open_orders: list[dict] = []
        self.placed: list[dict] = []
        self._seq = 9000

    def banner(self):
        return "[REC]"

    # ── account ──
    def get_positions(self):
        self.calls.append("get_positions")
        return list(self.positions)

    # ── market ──
    def get_ticker(self, symbol=None):
        self.calls.append(("get_ticker", symbol))
        return {"mark_price": 100.0, "last": 100.0}

    def get_contract(self, symbol=None):
        self.calls.append(("get_contract", symbol))
        return ContractMeta("BTC_USDT", 0.0001, 1, 0.1, 100)

    # ── orders ──
    def is_dual_position_mode(self):
        self.calls.append("is_dual_position_mode")
        return False

    def get_position_mode(self):
        self.calls.append("get_position_mode")
        return "single"

    def close_position(self, contract, side=None, size=0):
        self.calls.append(("close_position", contract, side, size))
        return {"id": "close-1", "fill_price": "100.0", "pnl": "1.5"}

    def list_orders(self, contract=None):
        self.calls.append(("list_orders", contract))
        return list(self.open_orders)

    def list_price_orders(self, contract=None):
        self.calls.append(("list_price_orders", contract))
        return list(self.price_orders)

    def cancel_price_order(self, pid):
        self.calls.append(("cancel_price_order", pid))
        return True

    def place_price_order(self, body):
        self._seq += 1
        o = {"id": self._seq, "status": "open",
             "initial": dict(body.get("initial") or {}),
             "trigger": dict(body.get("trigger") or {})}
        self.price_orders.append(o)
        self.placed.append(body)
        return dict(o)


def _ex(client: RecorderClient, tmp: str, **kw) -> Executor:
    kw.setdefault("label_prefix", "pt")
    return Executor(client, root=Path(tmp), bot_id="paper-test", **kw)


# ─────────────────────────────────────────────────────────────
# 一、配置层
# ─────────────────────────────────────────────────────────────
class TestConfigDefaults(unittest.TestCase):
    def test_defaults_are_all_on(self):
        """默认全部放行 —— 不改变任何存量 bot 的行为。"""
        bot = BotConfig(bot_id="t", env="paper", symbols=["BTC_USDT"])
        self.assertTrue(bot.manage_positions)
        self.assertTrue(bot.allow_close)
        self.assertTrue(bot.allow_reduce)

    def test_yaml_explicit_off_parses(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config" / "bots"
            cfg.mkdir(parents=True)
            (cfg / "t1.yaml").write_text(
                "bot_id: t1\nenv: paper\nsymbols: [\"BTC_USDT\"]\n"
                "manage_positions: false\nallow_close: false\nallow_reduce: false\n",
                encoding="utf-8")
            bot = load_bot_config(cfg / "t1.yaml")
            self.assertFalse(bot.manage_positions)
            self.assertFalse(bot.allow_close)
            self.assertFalse(bot.allow_reduce)

    def test_yaml_absent_keeps_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config" / "bots"
            cfg.mkdir(parents=True)
            (cfg / "t1.yaml").write_text(
                "bot_id: t1\nenv: paper\nsymbols: [\"BTC_USDT\"]\n", encoding="utf-8")
            bot = load_bot_config(cfg / "t1.yaml")
            self.assertTrue(bot.manage_positions)
            self.assertTrue(bot.allow_close)
            self.assertTrue(bot.allow_reduce)


# ─────────────────────────────────────────────────────────────
# 二、闸门判据（_management_gate）
# ─────────────────────────────────────────────────────────────
class TestManagementGate(unittest.TestCase):
    def _gate(self, action: str, **kw) -> str:
        with tempfile.TemporaryDirectory() as d:
            ex = _ex(RecorderClient(), d, **kw)
            return ex._management_gate(action)

    def test_default_allows_everything(self):
        self.assertEqual(self._gate("close"), "")
        self.assertEqual(self._gate("close_all"), "")
        self.assertEqual(self._gate("modify_tp_sl"), "")
        self.assertEqual(self._gate("trail"), "")

    def test_manage_positions_off_blocks_all_management(self):
        for act in ("close", "close_all", "flatten", "reduce", "reduce_long",
                    "reduce_short", "trail", "modify_tp_sl"):
            with self.subTest(action=act):
                msg = self._gate(act, manage_positions=False)
                self.assertTrue(msg)
                self.assertIn("manage_positions", msg)

    def test_manage_positions_off_keeps_entries_and_cancels(self):
        """只开仓不管仓 ≠ 不能交易：开仓/撤单必须放行。"""
        for act in ("open_long", "open_short", "stop_entry_long",
                    "stop_entry_short", "cancel_all", "cancel_price_all",
                    "cancel_trail_all", "hold"):
            with self.subTest(action=act):
                self.assertEqual(self._gate(act, manage_positions=False), "")

    def test_allow_close_off_only_blocks_closes(self):
        for act in ("close", "close_all", "flatten"):
            with self.subTest(action=act):
                self.assertIn("allow_close", self._gate(act, allow_close=False))
        # 减仓与改保护不受 allow_close 影响
        for act in ("reduce", "reduce_long", "reduce_short",
                    "modify_tp_sl", "trail"):
            with self.subTest(action=act):
                self.assertEqual(self._gate(act, allow_close=False), "")

    def test_allow_reduce_off_only_blocks_reduces(self):
        for act in ("reduce", "reduce_long", "reduce_short"):
            with self.subTest(action=act):
                self.assertIn("allow_reduce", self._gate(act, allow_reduce=False))
        for act in ("close", "close_all", "modify_tp_sl", "trail"):
            with self.subTest(action=act):
                self.assertEqual(self._gate(act, allow_reduce=False), "")

    def test_gate_resolves_requested_action_alias(self):
        """reduce_* 经 expand_signal 落成 action=close —— 必须按 requested_action 判。

        否则 allow_reduce=false 的配置会被 action="close" 悄悄放行。
        """
        with tempfile.TemporaryDirectory() as d:
            ex = _ex(RecorderClient(), d, allow_reduce=False)
            intent = Intent(action="close", symbol="BTC_USDT",
                            meta={"requested_action": "reduce_long"})
            self.assertTrue(ex._management_gate(intent.action, intent),
                            "reduce_long 别名必须命中 reduce 闸门")
            # 普通 close 不受 allow_reduce 影响
            self.assertEqual(ex._management_gate("close", Intent(action="close")), "")


# ─────────────────────────────────────────────────────────────
# 三、执行层（_execute_intent）
# ─────────────────────────────────────────────────────────────
class TestExecuteIntentGated(unittest.TestCase):
    def _run(self, intent: Intent, **kw):
        client = RecorderClient()
        with tempfile.TemporaryDirectory() as d:
            ex = _ex(client, d, **kw)
            step = ex._execute_intent(intent)
        return step, client

    def test_close_blocked_touches_nothing(self):
        step, client = self._run(
            Intent(action="close", symbol="BTC_USDT"), manage_positions=False)
        self.assertTrue(step.ok, "策略跳过是良性跳过，不能算整轮失败（否则触发回滚）")
        self.assertIn("gate_skipped", step.detail)
        self.assertIn("manage_positions", step.detail["gate_skipped"])
        self.assertEqual(client.calls, [], "被拒的平仓不许产生任何交易所调用")

    def test_close_all_blocked(self):
        step, client = self._run(
            Intent(action="close_all", symbol="BTC_USDT"), manage_positions=False)
        self.assertTrue(step.ok)
        self.assertEqual(client.calls, [])

    def test_reduce_alias_blocked(self):
        step, client = self._run(
            Intent(action="close", symbol="BTC_USDT",
                   meta={"requested_action": "reduce_long"}), manage_positions=False)
        self.assertTrue(step.ok)
        self.assertIn("manage_positions", step.detail["gate_skipped"])
        self.assertEqual(client.calls, [])

    def test_modify_tp_sl_blocked(self):
        step, client = self._run(
            Intent(action="modify_tp_sl", symbol="BTC_USDT"), manage_positions=False)
        self.assertTrue(step.ok)
        self.assertEqual(client.calls, [])

    def test_trail_blocked(self):
        step, client = self._run(
            Intent(action="trail", symbol="BTC_USDT"), manage_positions=False)
        self.assertTrue(step.ok)
        self.assertEqual(client.calls, [])

    def test_allow_close_off_blocks_close_but_not_reduce(self):
        step, client = self._run(
            Intent(action="close", symbol="BTC_USDT"), allow_close=False)
        self.assertTrue(step.ok)
        self.assertIn("allow_close", step.detail["gate_skipped"])
        self.assertEqual(client.calls, [])

        step2, client2 = self._run(
            Intent(action="close", symbol="BTC_USDT",
                   meta={"requested_action": "reduce_long"}),
            allow_close=False, allow_reduce=True)
        self.assertTrue(step2.ok)
        self.assertNotIn("gate_skipped", step2.detail)
        # reduce_long 别名 → _close 把 side 固化成 long（见 _close 的 requested 分支）
        self.assertIn(("close_position", "BTC_USDT", "long", 0), client2.calls)

    def test_allow_reduce_off_blocks_reduce_but_not_close(self):
        step, client = self._run(
            Intent(action="close", symbol="BTC_USDT",
                   meta={"requested_action": "reduce_long"}), allow_reduce=False)
        self.assertTrue(step.ok)
        self.assertIn("allow_reduce", step.detail["gate_skipped"])
        self.assertEqual(client.calls, [])

    def test_control_close_executes_when_enabled(self):
        step, client = self._run(Intent(action="close", symbol="BTC_USDT"))
        self.assertTrue(step.ok)
        self.assertIn(("close_position", "BTC_USDT", None, 0), client.calls)


class TestGateIsBenignSkipInBatch(unittest.TestCase):
    """闸门跳过必须是良性的：不 break 后续 intent、不触发回滚。"""

    def test_skipped_close_does_not_fail_batch(self):
        client = RecorderClient()
        with tempfile.TemporaryDirectory() as d:
            ex = _ex(client, d, manage_positions=False)
            signal = SignalFile(intents=[
                Intent(action="close", symbol="BTC_USDT"),
                Intent(action="cancel_all", symbol="BTC_USDT"),
            ])
            report = ex.execute_signal(signal)
        self.assertTrue(report.ok, "策略性跳过不能让整轮失败")
        actions = [r.action for r in report.results]
        self.assertIn("close", actions)
        self.assertIn("cancel_all", actions)
        close_step = next(r for r in report.results if r.action == "close")
        self.assertIn("gate_skipped", close_step.detail)
        # 回滚路径没有被触发（没有任何撤单调用）
        self.assertEqual([c for c in client.calls if c[0] == "cancel_price_order"], [])


# ─────────────────────────────────────────────────────────────
# 四、watcher 三条 sweep（绕过 _execute_intent 的路径）
# ─────────────────────────────────────────────────────────────
class _SweepClient(RecorderClient):
    """带持仓与 ATR 口径的桩（ensure_protection / peak_trail / give_back 用）。"""

    def __init__(self, mark=85000.0):
        super().__init__()
        self.mark = mark
        self._atr = 1.0

    def get_ticker(self, symbol=None):
        self.calls.append(("get_ticker", symbol))
        return {"mark_price": self.mark, "last": self.mark}

    def set_position(self, size):
        self.positions = [] if not size else [
            {"contract": "BTC_USDT", "size": size, "entry_price": 84000}
        ]

    def get_candles(self, *a, **kw):
        # _atr_pct 走 k 线；给一段恒定价 → ATR=0 → no atr（不影响闸门断言）
        return []


class TestSweepGates(unittest.TestCase):
    def _make(self, tmp, client, **kw):
        kw.setdefault("label_prefix", "pt")
        return Executor(client, root=Path(tmp), bot_id="paper-test", **kw)

    def test_ensure_protection_off_under_manage_positions_false(self):
        client = _SweepClient()
        with tempfile.TemporaryDirectory() as d:
            ex = self._make(d, client, manage_positions=False,
                            account_risk={"auto_protect": True,
                                          "auto_protect_sl_pct": 2.0})
            client.set_position(10)
            out = ex.ensure_protection("BTC_USDT")
            self.assertEqual(out["skipped"], "manage_positions_off")
            self.assertEqual(client.placed, [])

    def test_ensure_protection_control_still_places(self):
        """对照：开着 manage_positions 时补保护照旧（不被本次改动碰坏）。"""
        client = _SweepClient()
        with tempfile.TemporaryDirectory() as d:
            ex = self._make(d, client,
                            account_risk={"auto_protect": True,
                                          "auto_protect_sl_pct": 2.0})
            client.set_position(10)
            out = ex.ensure_protection("BTC_USDT")
            self.assertIn("placed", out)

    def test_peak_trail_off_under_manage_positions_false(self):
        client = _SweepClient()
        with tempfile.TemporaryDirectory() as d:
            ex = self._make(d, client, manage_positions=False,
                            account_risk={"peak_trail": True,
                                          "peak_trail_atr": 1.0})
            client.set_position(10)
            out = ex.check_peak_trail("BTC_USDT")
            self.assertEqual(out["skipped"], "manage_positions_off")
            self.assertEqual(client.placed, [])

    def test_give_back_off_under_manage_positions_false(self):
        client = _SweepClient()
        with tempfile.TemporaryDirectory() as d:
            ex = self._make(d, client, manage_positions=False,
                            account_risk={"give_back": True})
            client.set_position(10)
            ex.remember_give_back("BTC_USDT", "long", 30)
            out = ex.check_give_back("BTC_USDT")
            self.assertEqual(out["skipped"], "manage_positions_off")
            self.assertEqual([c for c in client.calls
                              if c[0] == "close_position"], [])

    def test_give_back_unaffected_by_allow_close_false(self):
        """解绑验证：`allow_close` 只管「AI 每轮的 close 指令」，不压 give_back。

        give_back 是「AI 开仓时预先授权阈值、程序后台执行」的回调锁盈平仓，
        由 `account_risk.give_back` 单独控制。本桩没有 owned SL → 会走到
        `no_owned_sl`（无法定义 1R），关键是**不再被 allow_close 拦下**。
        """
        client = _SweepClient()
        with tempfile.TemporaryDirectory() as d:
            ex = self._make(d, client, allow_close=False,
                            account_risk={"give_back": True})
            client.set_position(10)
            ex.remember_give_back("BTC_USDT", "long", 30)
            out = ex.check_give_back("BTC_USDT")
            self.assertNotEqual(out.get("skipped"), "close_disabled")
            self.assertEqual(out["skipped"], "no_owned_sl")

    def test_watcher_sweep_respects_manage_positions(self):
        """接线验证：`_auto_protect_sweep` 构造的 Executor 必须带上闸门。

        历史上栽过「闸门测试全绿而生产路径没接线」—— watcher 有 7 处
        Executor 构造点，这里至少覆盖 auto_protect 这条。
        """
        from omnialpha.watcher import ProjectPaths, _auto_protect_sweep

        with tempfile.TemporaryDirectory() as d:
            bot = BotConfig(
                bot_id="paper-test", env="paper", symbols=["BTC_USDT"],
                label_prefix="pt", manage_positions=False,
                account_risk={"auto_protect": True, "auto_protect_sl_pct": 2.0},
            )
            client = _SweepClient()
            client.set_position(10)
            bot.create_client = lambda: client
            paths = ProjectPaths(root=Path(d))
            self.assertEqual(_auto_protect_sweep(bot, paths, {}), 0)
            self.assertEqual(client.placed, [])


# ─────────────────────────────────────────────────────────────
# 五、可见性层（快照 / account 工具）
# ─────────────────────────────────────────────────────────────
class _SnapClient:
    """collect_snapshot / run_tool(account) 的最小客户端。"""

    def __init__(self, positions=None):
        self._positions = list(positions or [])
        self.pos_calls = 0

    def get_ticker(self, sym):
        return {"last": "84000"}

    def get_last_price(self, sym):
        return 84000.0

    def get_contract(self, sym):
        from types import SimpleNamespace
        return SimpleNamespace(quanto_multiplier=0.0001, order_size_round=0,
                               order_price_round=0.1, leverage_max=20)

    def get_contract_stats(self, sym, limit=1):
        return []

    def get_orderbook_top(self, sym, limit=5):
        return {"bids": [], "asks": []}

    def get_account(self):
        return {"available": "100.0", "total": "100.0", "position_mode": "dual"}

    def get_positions(self):
        self.pos_calls += 1
        return self._positions

    def list_orders(self):
        return []

    def list_price_orders(self, sym):
        return []


BTC_POS = {"contract": "BTC_USDT", "size": 19, "entry_price": "84000",
           "mode": "dual", "leverage": 20}


class TestSnapshotVisibilityGate(unittest.TestCase):
    def test_positions_hidden_when_disabled(self):
        client = _SnapClient(positions=[BTC_POS])
        snap = collect_snapshot(client, ["BTC_USDT"], candles=5, interval="15m",
                                manage_positions=False)
        acct = snap["account"]
        self.assertEqual(acct["positions"], [])
        self.assertEqual(acct["position_management"], "disabled")
        self.assertIn("manage_positions", acct["position_management_note"])
        self.assertEqual(client.pos_calls, 0, "关闸时不该去查持仓")

    def test_protections_hidden_when_disabled(self):
        """保护单同属持仓管理面 —— 不注入，否则模型仍会去 modify_tp_sl。"""
        client = _SnapClient(positions=[BTC_POS])
        snap = collect_snapshot(client, ["BTC_USDT"], candles=5, interval="15m",
                                manage_positions=False)
        self.assertEqual(snap["account"]["protections"], [])

    def test_positions_visible_by_default(self):
        client = _SnapClient(positions=[BTC_POS])
        snap = collect_snapshot(client, ["BTC_USDT"], candles=5, interval="15m")
        self.assertEqual(len(snap["account"]["positions"]), 1)
        self.assertNotIn("position_management", snap["account"])


class TestAccountToolVisibilityGate(unittest.TestCase):
    def test_account_tool_hides_positions_when_disabled(self):
        client = _SnapClient(positions=[BTC_POS])
        res = run_tool(client, "account", {}, manage_positions=False)
        self.assertEqual(res["positions"], [])
        self.assertEqual(res["protections"], [])
        self.assertIn("manage_positions", res.get("note", ""))
        self.assertEqual(client.pos_calls, 0)

    def test_account_tool_visible_by_default(self):
        client = _SnapClient(positions=[BTC_POS])
        res = run_tool(client, "account", {})
        self.assertEqual(len(res["positions"]), 1)


if __name__ == "__main__":
    unittest.main()
