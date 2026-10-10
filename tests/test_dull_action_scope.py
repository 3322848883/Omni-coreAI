# -*- coding: utf-8 -*-
"""T9：钝动作作用面（`scope`）+ 归属判据 + `replace=all` + 回滚通知。

覆盖 spec `docs/compose/spec/symbol-as-parameter.md` §S2.4⑧：
  1. `close_all`/`cancel_all`/`cancel_price_all`/`cancel_trail_all`：`symbol=""` 且未显式
     给 `scope` → **拒绝**（不再静默放大到全账户）；显式 `scope: bot|account` 才走宽路径。
  2. `close_all` 即使 `scope: account` 也只平**本 bot 有归属**的仓；判不了归属 → 不平，
     并落 `skipped_unattributed`。
  3. `_resync_protectors` / `_cleanup_orphan_protectors` 的 `label_prefix` 处理统一
     fail-closed（前缀为空不得退化为整表）。
  4. `replace=all` 真正实现（本 bot 命名空间内的**全 symbol** 旧单），`replace=symbol` 不变。
  5. `own_tag == "signal"` 不再静默退回整表撤单。
  6. 风控 raise 文案带 symbol。
  7. 回滚的腿落 report，通知渲染成「已回滚」而不是「已下单」。

**全部从生产分发点 `Executor.execute_signal(parse_signal(...))` 触发** —— 只单测 helper
不构成「生产路径已接线」的证据（本仓踩过：闸门测试全绿但生产路径根本没接线）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.monitoring.notify import format_trade_card, format_trade_steps  # noqa: E402
from omnialpha.schema import parse_signal  # noqa: E402


class _Meta:
    quanto_multiplier = 0.0001      # 与 BTC_USDT 同量级：名义 → 张数换算需要它
    order_size_round = 1
    order_size_min = 1
    order_price_round = 0.1
    price_tick = 0.1
    tick_size = 0.1
    lot_size = 1
    min_notional_usd = 1.0
    name = "BTC_USDT"


class _FakeClient:
    """最小桩：持仓/挂单/条件单 + 记录每一次平仓与撤单。"""

    def __init__(self, positions=None, orders=None, price_orders=None):
        self.positions = [dict(p) for p in (positions or [])]
        self.orders = [dict(o) for o in (orders or [])]
        self.price_orders = [dict(p) for p in (price_orders or [])]
        self.closed: list = []          # close_position(contract, side)
        self.cancelled: list = []       # ("order"|"price"|"trail", id)
        self.wiped: list = []           # 整表撤单（cancel_all_orders / cancel_all_price_orders）
        self._seq = 0

    def banner(self):
        return ""

    # ── 行情 / 合约 / 账户 ──
    def get_last_price(self, symbol):
        return 50000.0

    def get_ticker(self, symbol):
        return {"mark_price": 50000.0}

    def get_klines(self, symbol, interval, limit=100):
        """高波动 K 线（ATR% ≈ 4.5%），用来触发 `_vol_adjust` 的缩放 note。"""
        return [{"o": 50000, "h": 50000 + i * 100, "l": 49000, "c": 50000, "v": 1}
                for i in range(20)]

    def get_contract(self, symbol):
        return _Meta()

    def get_account(self):
        return {"total": 1000.0, "available": 1000.0}

    def get_positions(self):
        return [dict(p) for p in self.positions]

    def is_dual_position_mode(self):
        return False

    def get_position_mode(self):
        return "single"

    # ── 挂单 ──
    def place_order(self, body):
        self._seq += 1
        row = {
            "id": f"o{self._seq}",
            "text": body.get("text") or "",
            "contract": body.get("contract"),
            "size": body.get("size"),
            "left": body.get("size"),
            "status": "open",
        }
        self.orders.append(row)
        return dict(row)

    def get_order(self, order_id):
        for o in self.orders:
            if str(o.get("id")) == str(order_id):
                return dict(o)
        return {"id": order_id, "status": "open", "left": 1}

    def list_orders(self, contract=None):
        out = []
        for o in self.orders:
            if not o.get("text"):
                continue
            if contract and o.get("contract") != contract:
                continue
            out.append(dict(o))
        return out

    def cancel_order(self, order_id):
        self.cancelled.append(("order", str(order_id)))
        return {"cancelled": order_id}

    def cancel_all_orders(self, contract):
        self.wiped.append(("orders", contract))
        return {"cancelled": contract}

    # ── 条件单 ──
    def place_price_order(self, body):
        self._seq += 1
        row = dict(body)
        row["id"] = f"p{self._seq}"
        self.price_orders.append(row)
        return dict(row)

    def get_price_order(self, order_id):
        return {"id": order_id, "status": "open"}

    def list_price_orders(self, contract=None):
        out = []
        for p in self.price_orders:
            init = p.get("initial") or {}
            sym = p.get("contract") or init.get("contract")
            text = str(init.get("text") or p.get("text") or "")
            if not text:
                continue
            if contract and sym != contract:
                continue
            ro = init.get("reduce_only") or p.get("reduce_only") or 0
            sz = init.get("size") or p.get("size") or 0
            out.append({
                "id": p.get("id"),
                "contract": sym,
                "text": text,
                "reduce_only": ro,
                "size": sz,
                "initial": {"text": text, "reduce_only": ro, "size": sz, "contract": sym},
                "trigger": p.get("trigger") or {},
                "status": p.get("status") or "untriggered",
            })
        return out

    def cancel_price_order(self, order_id):
        self.cancelled.append(("price", str(order_id)))
        return {"cancelled": order_id}

    def cancel_all_price_orders(self, contract=None):
        self.wiped.append(("price_orders", contract))
        return {"cancelled": contract or "ALL"}

    # ── 平仓 / 追踪 ──
    def close_position(self, contract, side=None, size=0):
        self.closed.append({"contract": contract, "side": side, "size": size})
        return {"id": f"c{len(self.closed)}", "contract": contract, "side": side}

    def place_trailing_order(self, body):
        return {"id": "t1", **body}

    def stop_trailing_orders(self, contract=None):
        self.cancelled.append(("trail", contract))
        return {"cancelled": contract or "ALL"}


def _ex(client, **kw):
    kw.setdefault("symbols_whitelist", ["BTC_USDT", "ETH_USDT"])
    kw.setdefault("label_prefix", "brk")
    return Executor(client, **kw)


class TestDullActionScope(unittest.TestCase):
    """① 空 symbol 未给 scope → 拒绝（不再静默全账户）。"""

    def test_close_all_without_symbol_and_scope_is_rejected(self):
        c = _FakeClient(positions=[{"contract": "BTC_USDT", "size": 1, "mode": "single"},
                                   {"contract": "ETH_USDT", "size": 2, "mode": "single"}])
        rep = _ex(c).execute_signal(parse_signal({"action": "close_all"}))
        self.assertFalse(rep.ok, "空 symbol 未给 scope 必须被拒")
        err = rep.results[0].error or ""
        self.assertIn("scope: account", err, "错误信息要告诉用户怎么写全账户")
        self.assertIn("scope: bot", err, "错误信息要告诉用户怎么写全 bot")
        self.assertEqual(c.closed, [], "不得静默平掉全账户")

    def test_cancel_actions_without_symbol_and_scope_are_rejected(self):
        for action in ("cancel_all", "cancel_price_all", "cancel_trail_all"):
            with self.subTest(action=action):
                c = _FakeClient(orders=[{"id": "o1", "text": "t-brk", "contract": "BTC_USDT",
                                         "size": 1, "left": 1}])
                rep = _ex(c).execute_signal(parse_signal({"action": action}))
                self.assertFalse(rep.ok, f"{action} 空 symbol 未给 scope 必须被拒")
                self.assertIn("scope: account", rep.results[0].error or "")
                self.assertEqual(c.cancelled, [], f"{action} 不得静默撤全账户")
                self.assertEqual(c.wiped, [], f"{action} 不得走整表撤单")

    def test_explicit_scope_symbol_without_symbol_is_rejected(self):
        c = _FakeClient()
        rep = _ex(c).execute_signal(parse_signal({"action": "close_all", "scope": "symbol"}))
        self.assertFalse(rep.ok)
        self.assertIn("symbol", rep.results[0].error or "")

    def test_symbol_path_unchanged_without_scope(self):
        """单币路径（带 symbol、不写 scope）逐字不变：照平那一个币。"""
        c = _FakeClient(positions=[{"contract": "BTC_USDT", "size": 1, "mode": "single"}])
        rep = _ex(c).execute_signal(parse_signal({"action": "close_all", "symbol": "BTC_USDT"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual([x["contract"] for x in c.closed], ["BTC_USDT"])
        self.assertNotIn("skipped_unattributed", rep.results[0].detail)

    def test_cancel_trail_all_symbol_path_unchanged(self):
        c = _FakeClient()
        rep = _ex(c).execute_signal(parse_signal({"action": "cancel_trail_all", "symbol": "BTC_USDT"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(c.cancelled, [("trail", "BTC_USDT")])


class TestCloseAllOwnership(unittest.TestCase):
    """② close_all 的归属判据（fail-closed）。"""

    def test_account_scope_only_closes_owned_positions(self):
        c = _FakeClient(
            positions=[{"contract": "BTC_USDT", "size": 1, "mode": "single"},
                       {"contract": "ETH_USDT", "size": 2, "mode": "single"}],
            # BTC：本 bot 的入场挂单 → 有归属；ETH：只有**别的 bot** 的保护单
            orders=[{"id": "o1", "text": "t-brk", "contract": "BTC_USDT", "size": 1, "left": 1}],
            price_orders=[{"id": "p1",
                           "initial": {"text": "t-ethb-sl", "contract": "ETH_USDT",
                                       "reduce_only": 1, "size": -2}}],
        )
        rep = _ex(c).execute_signal(parse_signal({"action": "close_all", "scope": "account"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual([x["contract"] for x in c.closed], ["BTC_USDT"],
                         "只平本 bot 有归属的仓 —— 他 bot 的 ETH 仓不能动")
        self.assertEqual(rep.results[0].detail.get("skipped_unattributed"), ["ETH_USDT"])

    def test_unattributed_position_is_skipped_and_reported(self):
        c = _FakeClient(positions=[{"contract": "BTC_USDT", "size": 1, "mode": "single"}])
        rep = _ex(c).execute_signal(parse_signal({"action": "close_all", "scope": "account"}))
        self.assertEqual(c.closed, [], "判不了归属 → 不平（fail-closed）")
        self.assertEqual(rep.results[0].detail.get("skipped_unattributed"), ["BTC_USDT"],
                         "被跳过的仓要留痕")
        # **一个都没平 ≠ 成功**：恒 `ok=True` 会让归档里看起来成功了，而账户上的仓
        # 一个没动 —— 静默 no-op 比报错危险（没人会去查一个「成功」的动作）。
        self.assertFalse(rep.ok, "全部被跳过 = 这次 close_all 没生效")
        self.assertIn("close_all", rep.results[0].error or "")

    def test_partial_skip_still_reports_success(self):
        """有平成的腿时仍算成功：部分跳过是正常的（其他币的仓不是我的）。"""
        c = _FakeClient(
            positions=[{"contract": "BTC_USDT", "size": 1, "mode": "single"},
                       {"contract": "ETH_USDT", "size": 2, "mode": "single"}],
            orders=[{"id": "o1", "text": "t-brk", "contract": "BTC_USDT", "size": 1, "left": 1}],
        )
        rep = _ex(c).execute_signal(parse_signal({"action": "close_all", "scope": "account"}))
        self.assertTrue(rep.ok, "有平成的腿 → 成功")
        self.assertEqual([x["contract"] for x in c.closed], ["BTC_USDT"])
        self.assertEqual(rep.results[0].detail.get("skipped_unattributed"), ["ETH_USDT"])

    def test_bot_scope_restricted_to_declared_universe(self):
        c = _FakeClient(
            positions=[{"contract": "BTC_USDT", "size": 1, "mode": "single"},
                       {"contract": "SOL_USDT", "size": 1, "mode": "single"}],
            orders=[{"id": "o1", "text": "t-brk", "contract": "BTC_USDT", "size": 1, "left": 1},
                    {"id": "o2", "text": "t-brk", "contract": "SOL_USDT", "size": 1, "left": 1}],
        )
        rep = _ex(c).execute_signal(parse_signal({"action": "close_all", "scope": "bot"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual([x["contract"] for x in c.closed], ["BTC_USDT"],
                         "scope=bot 只在本 bot 声明的宇宙内动作")


class TestCancelOwnership(unittest.TestCase):
    """⑤ own_tag == 'signal' 不再静默退回整表。"""

    def test_default_label_no_longer_wipes_the_book(self):
        c = _FakeClient(orders=[
            {"id": "o1", "text": "t-signal", "contract": "BTC_USDT", "size": 1, "left": 1},
            {"id": "o2", "text": "t-signal-sl", "contract": "BTC_USDT", "size": -1, "left": -1},
            {"id": "o3", "text": "t-other", "contract": "BTC_USDT", "size": 1, "left": 1},
        ])
        ex = Executor(c, symbols_whitelist=["BTC_USDT"], order_scope="own")  # 无 label_prefix
        rep = ex.execute_signal(parse_signal({"action": "cancel_all", "symbol": "BTC_USDT"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(sorted(c.cancelled), [("order", "o1"), ("order", "o2")],
                         "只撤本 bot 命名空间（t-signal*），别家的单不能动")
        self.assertEqual(c.wiped, [], "不得退回整表撤单")

    def test_price_orders_default_label_no_longer_wipes_the_book(self):
        c = _FakeClient(price_orders=[
            {"id": "p1", "initial": {"text": "t-signal-tp", "contract": "BTC_USDT",
                                     "reduce_only": 1, "size": -1}},
            {"id": "p2", "initial": {"text": "t-other-tp", "contract": "BTC_USDT",
                                     "reduce_only": 1, "size": -1}},
        ])
        ex = Executor(c, symbols_whitelist=["BTC_USDT"], order_scope="own")
        rep = ex.execute_signal(parse_signal({"action": "cancel_price_all", "symbol": "BTC_USDT"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(c.cancelled, [("price", "p1")])
        self.assertEqual(c.wiped, [], "不得退回整表撤单")

    def test_account_scope_cancels_owned_orders_on_every_symbol(self):
        c = _FakeClient(orders=[
            {"id": "o1", "text": "t-brk", "contract": "BTC_USDT", "size": 1, "left": 1},
            {"id": "o2", "text": "t-brk", "contract": "ETH_USDT", "size": 1, "left": 1},
            {"id": "o3", "text": "t-other", "contract": "ETH_USDT", "size": 1, "left": 1},
        ])
        rep = _ex(c).execute_signal(parse_signal({"action": "cancel_all", "scope": "account"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(sorted(c.cancelled), [("order", "o1"), ("order", "o2")],
                         "宽路径（显式 scope）才收全账户上本 bot 的单")
        self.assertEqual(c.wiped, [], "own 命名空间下不得整表撤单")


class TestWideScopeIsExecuted(unittest.TestCase):
    """宽路径（`order_scope: all`）的**闸门与执行必须同源**。

    `scope` 只收窄了「查保护单」那一步、真正撤单仍打全账户，等于收紧没做 ——
    这正是本仓记录过的「闸门测试全绿但生产路径没接线」形态。
    """

    def _ex_all(self, c):
        return Executor(c, symbols_whitelist=["BTC_USDT"], order_scope="all")

    def _two_symbols(self):
        return _FakeClient(price_orders=[
            {"id": "p1", "initial": {"text": "t-brk-tp", "contract": "BTC_USDT",
                                     "reduce_only": 1, "size": -1}},
            {"id": "p2", "initial": {"text": "t-brk-tp", "contract": "SOL_USDT",
                                     "reduce_only": 1, "size": -1}},
        ])

    def test_bot_scope_price_wipe_is_limited_to_universe(self):
        c = self._two_symbols()
        rep = self._ex_all(c).execute_signal(
            parse_signal({"action": "cancel_price_all", "scope": "bot"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(c.wiped, [("price_orders", "BTC_USDT")],
                         "scope=bot 只能撤本 bot 宇宙内的条件单")

    def test_account_scope_price_wipe_covers_every_symbol(self):
        c = self._two_symbols()
        rep = self._ex_all(c).execute_signal(
            parse_signal({"action": "cancel_price_all", "scope": "account"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(c.wiped, [("price_orders", "BTC_USDT"), ("price_orders", "SOL_USDT")])

    def test_symbol_scope_price_wipe_unchanged(self):
        """带 symbol 的老路径逐字不变（单币 bot 走这条）。"""
        c = self._two_symbols()
        rep = self._ex_all(c).execute_signal(
            parse_signal({"action": "cancel_price_all", "symbol": "BTC_USDT"}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(c.wiped, [("price_orders", "BTC_USDT")])


class TestReplaceAll(unittest.TestCase):
    """④ replace=all 真正实现（且按归属过滤）。"""

    def test_replace_all_collects_bot_orders_outside_payload(self):
        c = _FakeClient(
            positions=[{"contract": "ETH_USDT", "size": -1, "mode": "dual_short"}],
            orders=[{"id": "o1", "text": "t-brk", "contract": "ETH_USDT", "size": 1, "left": 1},
                    {"id": "o2", "text": "t-other", "contract": "ETH_USDT", "size": 1, "left": 1}],
            # 本 bot 的 SL 正在保护真实空仓（+1 平空）→ 绝不能被 replace 撤掉
            price_orders=[{"id": "p1", "initial": {"text": "t-brk-sl", "contract": "ETH_USDT",
                                                   "reduce_only": 1, "size": 1}}],
        )
        rep = _ex(c).execute_signal(parse_signal(
            {"replace": "all", "orders": [{"action": "hold"}]}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertIn(("order", "o1"), c.cancelled,
                      "payload 之外的 symbol 上本 bot 的旧单也要收（replace=all 的真义）")
        self.assertNotIn(("order", "o2"), c.cancelled, "别家的单不能碰")
        self.assertNotIn(("price", "p1"), c.cancelled, "保护真实持仓的 SL 不能撤")

    def test_replace_symbol_leaves_other_symbols_alone(self):
        c = _FakeClient(orders=[
            {"id": "o1", "text": "t-brk", "contract": "BTC_USDT", "size": 1, "left": 1},
            {"id": "o2", "text": "t-brk", "contract": "ETH_USDT", "size": 1, "left": 1},
        ])
        rep = _ex(c).execute_signal(parse_signal(
            {"replace": "symbol", "orders": [{"action": "hold", "symbol": "BTC_USDT"}]}))
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertIn(("order", "o1"), c.cancelled)
        self.assertNotIn(("order", "o2"), c.cancelled,
                         "replace=symbol 只动本轮 payload 触及的币（行为不变）")


class TestProtectorNamespace(unittest.TestCase):
    """③ label_prefix 处理统一 fail-closed。"""

    def _orphan_client(self):
        # 真实保护单必然带 trigger（重挂要用它）；桩缺了它会让 `_resync_protectors`
        # 走「无法重挂 → 不撤（宁可不撤也不裸仓）」的保守分支，测不到归属过滤。
        return _FakeClient(
            price_orders=[
                {"id": "p1", "initial": {"text": "t-brk-tp", "contract": "BTC_USDT",
                                         "reduce_only": 1, "size": -10},
                 "trigger": {"price": "60000", "rule": 2}},
                {"id": "p2", "initial": {"text": "t-other-tp", "contract": "BTC_USDT",
                                         "reduce_only": 1, "size": -10},
                 "trigger": {"price": "60000", "rule": 2}},
            ])

    def test_resync_fails_closed_without_namespace(self):
        c = _FakeClient(positions=[{"contract": "BTC_USDT", "size": 13, "mode": "single"}],
                        price_orders=self._orphan_client().price_orders)
        ex = Executor(c, symbols_whitelist=["BTC_USDT"])  # 无 label_prefix
        with self.assertLogs("omnialpha.executor", level="WARNING") as cm:
            notes = ex._resync_protectors("BTC_USDT")
        self.assertEqual(c.cancelled, [], "前缀为空不得退化为整表")
        self.assertEqual(len(c.price_orders), 2, "不得重挂任何保护单")
        self.assertTrue(any("skipped_unattributed" in str(n) for n in notes),
                        f"被跳过的数量要记录：{notes}")
        self.assertTrue(any("skipped_unattributed" in m for m in cm.output))

    def test_cleanup_fails_closed_without_namespace(self):
        c = self._orphan_client()
        ex = Executor(c, symbols_whitelist=["BTC_USDT"])
        with self.assertLogs("omnialpha.executor", level="WARNING") as cm:
            cancelled = ex._cleanup_orphan_protectors("BTC_USDT")
        self.assertEqual(cancelled, [])
        self.assertEqual(c.cancelled, [], "前缀为空不得退化为整表")
        self.assertTrue(any("skipped_unattributed" in m for m in cm.output))

    def test_resync_uses_bot_label_when_no_prefix(self):
        c = _FakeClient(positions=[{"contract": "BTC_USDT", "size": 13, "mode": "single"}],
                        price_orders=self._orphan_client().price_orders)
        ex = Executor(c, symbols_whitelist=["BTC_USDT"])  # 无 label_prefix，但有 label
        notes = ex._resync_protectors("BTC_USDT", label="brk")
        self.assertEqual([x for x in c.cancelled if x[0] == "price"], [("price", "p1")],
                         "只处理本 bot 的 label 命名空间")
        self.assertTrue(any("resized" in str(n) for n in notes), notes)

    def test_cleanup_uses_bot_label_when_no_prefix(self):
        c = self._orphan_client()
        ex = Executor(c, symbols_whitelist=["BTC_USDT"])
        cancelled = ex._cleanup_orphan_protectors("BTC_USDT", label="brk")
        self.assertEqual(cancelled, ["p1"])
        self.assertEqual(c.cancelled, [("price", "p1")], "别家的命名空间不碰")


class TestRiskMessagesCarrySymbol(unittest.TestCase):
    """⑥ 风控 raise 文案带 symbol（只加信息，不改判定）。"""

    def test_max_notional_pct_carries_symbol(self):
        c = _FakeClient()
        ex = _ex(c, max_notional_usd=100000,
                 account_risk={"max_notional_pct": 0.5, "risk_pct": 0.01},
                 require_sl=False)
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size_usd": 8000, "sl": 49900,
        }))
        self.assertFalse(rep.ok)
        self.assertIn("MAX_NOTIONAL_PCT", rep.results[0].error or "")
        self.assertIn("BTC_USDT", rep.results[0].error or "",
                      "风控文案不带 symbol → 归档顶层 error 无法归因")

    def test_no_flip_carries_symbol(self):
        c = _FakeClient(positions=[{"contract": "BTC_USDT", "size": 1, "mode": "single"}])
        rep = _ex(c).execute_signal(parse_signal({
            "action": "open_short", "symbol": "BTC_USDT", "size": 1, "sl": 60000,
        }))
        self.assertFalse(rep.ok)
        self.assertIn("NO_FLIP", rep.results[0].error or "")
        self.assertIn("BTC_USDT", rep.results[0].error or "")

    def test_trigger_price_side_carries_symbol(self):
        """预检（`_precheck_exit_triggers`）拒单文案也要带币。"""
        c = _FakeClient()
        ex = _ex(c, require_sl=True)
        # mark=50000；open_long 的 SL 要 rule=2（跌破触发）→ 51000 落在非法一侧
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT", "size": 1, "sl": 51000,
        }))
        self.assertFalse(rep.ok, rep.to_dict())
        err = rep.results[0].error or ""
        self.assertIn("TRIGGER_PRICE_SIDE", err)
        self.assertIn("BTC_USDT", err)

    def test_size_notes_carry_symbol(self):
        """仓位验算/波动率缩放的 note 逐段都要带币（多币下才能归因）。"""
        c = _FakeClient()
        ex = _ex(c, max_notional_usd=1e12,
                 account_risk={"max_notional_pct": 10, "risk_pct": 0.01,
                               "vol_target_pct": 2.0},
                 require_sl=True)
        rep = ex.execute_signal(parse_signal({
            "action": "open_long", "symbol": "BTC_USDT",
            "size_usd": 100, "price": 50000, "sl": 25000,
        }))
        self.assertTrue(rep.ok, rep.to_dict())
        note = rep.results[0].detail.get("size_align_note") or ""
        self.assertIn("size_clamped", note)
        self.assertIn("vol_adjust", note)
        parts = [p.strip() for p in note.split(";") if p.strip()]
        for p in parts:
            self.assertIn("BTC_USDT", p, f"这段 note 不带 symbol：{p!r}")

    def test_safe_mode_carries_symbol(self):
        """安全模式（`_check_safe_mode`）文案带币。"""
        import json
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "data" / "bots" / "b1" / "state"
            state.mkdir(parents=True, exist_ok=True)
            (state / "health.json").write_text(
                json.dumps({"error_streak": 3}), encoding="utf-8")
            c = _FakeClient()
            ex = Executor(c, symbols_whitelist=["BTC_USDT"], label_prefix="brk",
                          root=Path(td), bot_id="b1", require_sl=True,
                          account_risk={"safe_mode_after_failures": 3})
            rep = ex.execute_signal(parse_signal({
                "action": "open_long", "symbol": "BTC_USDT", "size": 1, "sl": 49000,
            }))
            self.assertFalse(rep.ok, rep.to_dict())
            err = rep.results[0].error or ""
            self.assertIn("SAFE_MODE", err)
            self.assertIn("BTC_USDT", err)


class TestRollbackNotification(unittest.TestCase):
    """⑦ 回滚留痕 + 通知与最终状态一致。"""

    def _failing_round(self, td):
        """生产路径造一次「有腿失败 → 回滚」：BTC 的入场单已挂出，ETH 那条被拒。"""
        c = _FakeClient()
        ex = Executor(c, symbols_whitelist=["BTC_USDT"], label_prefix="brk",
                      root=Path(td), bot_id="b1", require_sl=False)
        rep = ex.execute_signal(parse_signal({"orders": [
            {"action": "open_long", "symbol": "BTC_USDT", "size": 1, "type": "market"},
            {"action": "open_long", "symbol": "ETH_USDT", "size": 1, "type": "market"},
        ]}))
        return c, rep

    def test_rollback_legs_are_recorded_in_report(self):
        with tempfile.TemporaryDirectory() as td:
            c, rep = self._failing_round(td)
        self.assertFalse(rep.ok)
        roll = [s for s in rep.results if s.action == "rollback"]
        self.assertTrue(roll, rep.to_dict())
        detail = roll[-1].detail or {}
        self.assertEqual(detail.get("rolled_back_symbols"), ["BTC_USDT"], detail)
        self.assertEqual(c.cancelled, [("order", "o1")], c.cancelled)
        self.assertEqual(
            [(leg.get("symbol"), leg.get("kind"), leg.get("id"))
             for leg in detail.get("rolled_back_legs") or []],
            [("BTC_USDT", "order", "o1")], detail)

    def test_notify_renders_rolled_back_leg_as_rolled_back(self):
        with tempfile.TemporaryDirectory() as td:
            _, rep = self._failing_round(td)
        steps = rep.to_dict()["steps"]
        cards = format_trade_card("b1", steps)
        btc = [c for c in cards if "BTC_USDT" in str(c)]
        self.assertTrue(btc, f"没有 BTC 的卡片：{cards}")
        title = btc[0]["header"]["title"]["content"]
        self.assertIn("已回滚", title, f"回滚掉的腿仍显示成「{title}」")
        self.assertNotIn("挂单", title)
        self.assertNotIn("开仓", title)
        # 文本版同源
        lines = [ln for ln in format_trade_steps("b1", steps) if "BTC_USDT" in ln]
        self.assertTrue(lines and all("已回滚" in ln for ln in lines), lines)


if __name__ == "__main__":
    unittest.main()
