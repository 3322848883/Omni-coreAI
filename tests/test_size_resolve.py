# -*- coding: utf-8 -*-
"""下单尺寸四口径统一入口（`Executor._resolve_order_size`）回归。

背景（2026-10-26）：线上这条线有三个独立缺陷把「模型给的量」弄错：

1. 策略层 `Chip.size` 是 `Optional[int]` + `_int()` 解析 → 模型写 `0.7` 张，
   `int(0.7)` 直接变 **0**（静默）；写 `"0.7"` 则抛 `PlanError`（整轮作废）。
2. 执行层 `contracts = int(intent.size)` → `0.7` 变 **0**，再被 `usd_to_contracts`
   的「不足 1 张抬到 1 张」兜底成 **1 张**（ETH 从 16.5U 变 23.25U，+41%）。
3. `usd_to_contracts` 用 `order_size_round`（交易所根本不提供，1061 个合约 0 命中，
   恒 1 是假值）当步长 → 永远只知道「1 张」这一档，ETH 的 0.1 步长不可见。

本文件把四口径（size / size_usd / size_pct / margin_pct）与「冲突/非法输入出声」
一起钉住。先红后绿。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor
from omnialpha.gate_client import ContractMeta, GateApiError
from omnialpha.schema import Intent

ETH = "ETH_USDT"
BTC = "BTC_USDT"
PX_ETH = 2325.0      # 1 张 = 23.25U（quanto 0.01）
PX_BTC = 60000.0     # 1 张 = 6U（quanto 0.0001）

META = {
    ETH: dict(quanto_multiplier=0.01, size_min=0.1, size_max=1e7, enable_decimal=True,
              order_price_round=0.01),
    BTC: dict(quanto_multiplier=0.0001, size_min=1.0, size_max=1e6, enable_decimal=False,
              order_price_round=0.1),
}


class FakeGate:
    env = "live"
    equity = 100.0

    def __init__(self):
        self.placed: list[dict] = []
        self.price_orders: dict[str, dict] = {}
        self.positions: list[dict] = []
        self._seq = 0

    def banner(self):
        return "[FAKE]"

    def get_contract(self, symbol):
        p = META[symbol]
        return ContractMeta(name=symbol, order_size_round=1, leverage_max=100, **p)

    def get_last_price(self, symbol):
        return PX_ETH if symbol == ETH else PX_BTC

    def get_ticker(self, symbol):
        return {"mark_price": self.get_last_price(symbol)}

    def get_account(self):
        return {"total": str(self.equity), "balance": str(self.equity)}

    def get_available_usdt(self):
        return self.equity

    def get_positions(self):
        return [dict(x) for x in self.positions]

    def get_position_mode(self):
        return "single"

    def set_leverage(self, *a, **k):
        return True

    def place_order(self, body):
        self._seq += 1
        self.placed.append(dict(body))
        self.last_order = {"id": str(self._seq), "status": "open",
                           "size": str(body.get("size")), "left": str(body.get("size"))}
        return dict(self.last_order)

    def place_price_order(self, body):
        self._seq += 1
        oid = str(self._seq)
        rec = {"id": oid, "status": "open", "initial": dict(body.get("initial") or {}),
               "trigger": dict(body.get("trigger") or {})}
        self.price_orders[oid] = rec
        self.placed.append(dict(body))
        return dict(rec)

    def list_orders(self, symbol=None):
        return []

    def list_price_orders(self, symbol=None):
        return []

    def get_order(self, oid):
        # 成交确认要读回真实状态：返回 {} 会让 `entry_not_confirmed` 误报
        return dict(getattr(self, "last_order", {}))

    def get_price_order(self, oid):
        return dict(self.price_orders.get(str(oid)) or {})

    def cancel_order(self, oid):
        return True

    def cancel_price_order(self, oid):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.gate = FakeGate()
        self.ex = Executor(self.gate, root=Path(self._td.name),
                           label_prefix="sz", bot_id="sz")

    def tearDown(self):
        self._td.cleanup()

    def resolve(self, symbol=ETH, **kw):
        meta = self.gate.get_contract(symbol)
        intent = Intent(action=kw.pop("action", "open_long"), symbol=symbol, **kw)
        return self.ex._resolve_order_size(intent, meta)


class TestExplicitSize(Base):
    """模型给**张数** = 精确意图 → 不可执行就出声，绝不静默放大。"""

    def test_fraction_passes_through(self):
        q, notional, basis = self.resolve(size=0.7)
        self.assertAlmostEqual(q, 0.7)
        self.assertAlmostEqual(notional, 0.7 * PX_ETH * 0.01, places=4)
        self.assertEqual(basis, "size")

    def test_floors_to_step(self):
        q, _, _ = self.resolve(size=0.15)       # step 0.1 → 0.1
        self.assertAlmostEqual(q, 0.1)

    def test_negative_size_takes_abs(self):
        q, _, _ = self.resolve(size=-2)         # 方向由 action 决定，不靠符号
        self.assertAlmostEqual(q, 2.0)

    def test_zero_size_is_rejected(self):
        with self.assertRaises(GateApiError) as ctx:
            self.resolve(size=0)
        self.assertIn("SIZE_INVALID", str(ctx.exception))

    def test_below_one_step_is_rejected_not_inflated(self):
        """ETH step 0.1：显式 0.05 张 → 拒单（抬到 0.1 等于放大 2 倍，不能静默）。"""
        with self.assertRaises(GateApiError) as ctx:
            self.resolve(size=0.05)
        self.assertIn("SIZE_TOO_SMALL", str(ctx.exception))

    def test_btc_integer_contract(self):
        q, _, _ = self.resolve(BTC, size=1.5)   # step 1 → 1
        self.assertAlmostEqual(q, 1.0)
        with self.assertRaises(GateApiError):
            self.resolve(BTC, size=0.5)          # step 1，0.5 不可执行


class TestUsdCentred(Base):
    """模型给**名义金额** = 系统换算 → 不足最小可下量时抬到 `size_min`（不是 1 张）。"""

    def test_eth_usd_17_gives_fraction(self):
        q, notional, basis = self.resolve(size_usd=17)
        self.assertAlmostEqual(q, 0.7)           # floor(17 / 23.25) = 0.7
        self.assertAlmostEqual(notional, 0.7 * 23.25, places=4)
        self.assertEqual(basis, "size_usd")

    def test_tiny_usd_lifts_to_size_min_not_one_contract(self):
        q, notional, _ = self.resolve(size_usd=1)   # 1U < 一个步长 2.325U
        self.assertAlmostEqual(q, 0.1, msg="应抬到 size_min=0.1，而不是 1 张（23.25U）")
        self.assertAlmostEqual(notional, 2.325, places=4)

    def test_btc_usd_17(self):
        q, _, _ = self.resolve(BTC, size_usd=17)    # floor(17 / 6) = 2
        self.assertAlmostEqual(q, 2.0)

    def test_size_pct_uses_available_equity(self):
        q, _, basis = self.resolve(size_pct=0.2)    # 100U * 0.2 = 20U → floor(0.86)=0.8
        self.assertAlmostEqual(q, 0.8)
        self.assertEqual(basis, "size_pct")

    def test_margin_pct_multiplies_leverage(self):
        q, _, basis = self.resolve(margin_pct=0.2, leverage=10)  # 20U*10=200U → 8.6 张
        self.assertAlmostEqual(q, 8.6)
        self.assertEqual(basis, "margin_pct")


class TestConflicts(Base):
    def test_size_and_usd_together_is_rejected(self):
        with self.assertRaises(GateApiError) as ctx:
            self.resolve(size=0.7, size_usd=17)
        self.assertIn("SIZE_CONFLICT", str(ctx.exception))

    def test_usd_and_pct_together_is_rejected(self):
        with self.assertRaises(GateApiError) as ctx:
            self.resolve(size_usd=17, size_pct=0.2)
        self.assertIn("SIZE_CONFLICT", str(ctx.exception))

    def test_nothing_given_is_rejected(self):
        with self.assertRaises(GateApiError) as ctx:
            self.resolve()
        self.assertIn("SIZE_REQUIRED", str(ctx.exception))


class TestOpenWiring(Base):
    """端到端：`_open` 真的把量化后的张数写进下单 body（不再 int 截断）。"""

    def _open(self, **kw):
        # 带止损：`require_sl` 默认开，缺 sl 会被 `_check_open_sl` 拦在前面
        kw.setdefault("sl", PX_ETH * 0.98)
        intent = Intent(action="open_long", symbol=ETH, order_type="market", **kw)
        # 手工构造的 Intent 绕过了 `parse_intent`，触发规则要自己补（真实链路由
        # `infer_trigger_rules` 落定，`_place_trigger` 里 `int(rule)` 不接受 None）
        intent.trigger_rule_sl = 2
        intent.trigger_rule_tp = 1
        return self.ex._open(intent)

    def test_explicit_fraction_reaches_exchange(self):
        res = self._open(size=0.7)
        self.assertTrue(getattr(res, "ok", False), getattr(res, "error", None))
        sizes = [abs(float(b["size"])) for b in self.gate.placed if "size" in b]
        self.assertIn(0.7, [round(s, 4) for s in sizes],
                      f"下单 body 里应出现 0.7 张，实际 {sizes}")

    def test_usd_path_lands_on_fraction(self):
        res = self._open(size_usd=17)
        self.assertTrue(getattr(res, "ok", False), getattr(res, "error", None))
        sizes = [abs(float(b["size"])) for b in self.gate.placed if "size" in b]
        self.assertIn(0.7, [round(s, 4) for s in sizes], f"实际 {sizes}")


class TestTpLegsByStep(Base):
    """多级止盈拆腿必须按步长：0.7 张拆两档不能变成 0 张 / 1 张。"""

    def _meta(self, symbol=ETH):
        return self.gate.get_contract(symbol)

    def test_single_leg_keeps_fraction(self):
        it = Intent(action="open_long", symbol=ETH, tp=2400.0)
        legs = self.ex._tp_legs(it, 0.7, self._meta())
        self.assertEqual(len(legs), 1)
        self.assertAlmostEqual(legs[0][1], 0.7)

    def test_two_legs_sum_to_position(self):
        it = Intent(action="open_long", symbol=ETH, tp=2400.0, tp2=2500.0)
        legs = self.ex._tp_legs(it, 0.7, self._meta())
        self.assertEqual(len(legs), 2, f"两档应拆两腿：{legs}")
        total = sum(sz for _, sz in legs)
        self.assertAlmostEqual(total, 0.7, msg=f"合计必须 = 持仓：{legs}")
        for _, sz in legs:
            self.assertGreaterEqual(sz, 0.1 - 1e-9, f"每腿 ≥ 一个步长：{legs}")

    def test_integer_position_unchanged(self):
        """BTC（step 1）6 张三档 → 2/2/2，与旧行为逐字一致。"""
        it = Intent(action="open_long", symbol=BTC, tp=61000.0, tp2=62000.0, tp3=63000.0)
        legs = self.ex._tp_legs(it, 6, self._meta(BTC))
        self.assertEqual([round(sz) for _, sz in legs], [2, 2, 2])

    def test_below_one_step_falls_back_to_single_leg(self):
        """0.2 张拆三档 → 拆不动，退回单腿全平（不许挂出超量 reduce-only）。"""
        it = Intent(action="open_long", symbol=ETH, tp=2400.0, tp2=2500.0, tp3=2600.0)
        legs = self.ex._tp_legs(it, 0.2, self._meta())
        self.assertEqual(len(legs), 1)
        self.assertAlmostEqual(legs[0][1], 0.2)


class TestGridTpFractional(Base):
    """`ensure_grid_tp`（服务器线独有的档位条件限价 TP）也不许按整数张算。

    原来三处 `_int_or_zero(size)`：多头持仓 0.7 张 → 0（误判「无多头持仓」→ 撤掉
    在盘 TP）；在盘 -gtp 已覆盖 0.7 → 0（覆盖恒 0 → 每轮重挂 → 超配被拒）；
    档位 0.3 张 → 0（整档挂不上）。
    """

    def setUp(self):
        super().setUp()
        self.ex.account_risk = dict(self.ex.account_risk or {}, grid_tp_auto="dry")
        self.ex.bot_id = "gtp"
        state = Path(self.ex.root) / "data" / "bots" / "gtp" / "state"
        state.mkdir(parents=True, exist_ok=True)
        import json
        (state / "ladder_levels.json").write_text(json.dumps({
            "levels": [
                {"symbol": ETH, "price": 2400.0, "size": 0.4,
                 "tp_target": 2420.0, "status": "filled"},
                {"symbol": ETH, "price": 2380.0, "size": 0.3,
                 "tp_target": 2400.0, "status": "filled"},
            ]
        }), encoding="utf-8")
        self.gate.positions = [{"contract": ETH, "size": "0.7",
                                "entry_price": "2390", "mark_price": "2390"}]

    def test_candidates_keep_fraction(self):
        out = self.ex.ensure_grid_tp(ETH)
        self.assertEqual(out.get("grid_tp"), "dry", out)
        sizes = [c["size"] for c in out.get("candidates") or []]
        self.assertEqual(len(sizes), 2, f"两档都应成为候选：{out}")
        self.assertAlmostEqual(sum(sizes), 0.7, msg=f"合计不得超过持仓 0.7：{sizes}")
        for sz in sizes:
            self.assertGreaterEqual(sz, 0.1 - 1e-9)

    def test_fractional_position_not_read_as_flat(self):
        out = self.ex.ensure_grid_tp(ETH)
        self.assertAlmostEqual(float(out.get("position") or 0), 0.7)
        self.assertNotIn("no_long_position", str(out.get("skipped") or ""))


class TestPlanLayerKeepsFraction(unittest.TestCase):
    """策略层 `Chip.size` 不许把 0.7 变成 0（这是 8.2h 空转的入口之一）。"""

    def test_chip_size_accepts_fraction(self):
        from omnialpha.strategist.schema import parse_plan_text
        plan = ('{"chips":[{"symbol":"ETH_USDT","action":"open_long",'
                '"size":0.7,"price":2300}]}')
        p = parse_plan_text(plan, symbols=["ETH_USDT"])
        self.assertAlmostEqual(float(p.chips[0].size), 0.7)

    def test_chip_size_string_fraction_does_not_raise(self):
        from omnialpha.strategist.schema import parse_plan_text
        plan = ('{"chips":[{"symbol":"ETH_USDT","action":"open_long",'
                '"size":"0.7","price":2300}]}')
        p = parse_plan_text(plan, symbols=["ETH_USDT"])
        self.assertAlmostEqual(float(p.chips[0].size), 0.7)

    def test_chip_size_garbage_still_raises_plan_error(self):
        from omnialpha.strategist.schema import PlanError, parse_plan_text
        plan = ('{"chips":[{"symbol":"ETH_USDT","action":"open_long",'
                '"size":"lots","price":2300}]}')
        with self.assertRaises(PlanError):
            parse_plan_text(plan, symbols=["ETH_USDT"])

    def test_chip_size_zero_is_kept_as_zero_not_none(self):
        from omnialpha.strategist.schema import parse_plan_text
        plan = ('{"chips":[{"symbol":"ETH_USDT","action":"open_long",'
                '"size":0,"price":2300}]}')
        p = parse_plan_text(plan, symbols=["ETH_USDT"])
        self.assertEqual(float(p.chips[0].size), 0.0, "0 要如实传给执行层去拒单，不能吞成 None")


if __name__ == "__main__":
    unittest.main()
