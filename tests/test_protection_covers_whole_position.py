# -*- coding: utf-8 -*-
"""保护单必须**覆盖整仓** —— 小数张 + 显式张数 = 必然残仓。

2026-10-28 实盘（pa-eth-v5 / GATE_API_KEY）：
  持仓 13.8 张 = **0.138 ETH**（`/positions.value` 324.31 / 2350.07 / 0.01）
  但 SL / TP 挂的是 **−13 张**（`initial.size`），触发后只平 13 张 →
  残 0.8 张 = 0.008 ETH 永久留在账户里。

根因链（换算逐层核对）：
  1. ETH_USDT `size_step = 0.1`、`enable_decimal = True` → 入场允许小数张
     `323U / (2340.5 × 0.01) = 13.8 张` → 成交 13.8 张（0.138 ETH）
  2. `position_qty()` 读真 = 13.8 ✓（`/positions.size` 是 int64，只回报 13）
  3. SL/TP 用 13.8 → `/price_orders` 的 `initial.size` 是 **int64**，
     `_int_price_order_size` 向零取整 → 13 ✗
  4. 触发只平 13 → 残 0.8 张；`reduce_only` 只减不增 ⇒ 残量永久化

修法：`_place_trigger` 里「带小数 且 覆盖整仓」→ 走 `auto_size`（close_long/
close_short，语义=全平该方向），与张数大小无关。**部分档（网格多档 TP）绝不能被
放大成整侧平仓** —— 判据是 `size >= 持仓`，不是「有小数」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.gate_client import ContractMeta  # noqa: E402
from omnialpha.schema import infer_trigger_rules, parse_signal  # noqa: E402

SYM = "ETH_USDT"
META = ContractMeta(name=SYM, quanto_multiplier=0.01, order_size_round=1,
                    order_price_round=0.01, leverage_max=100,
                    size_min=0.1, size_max=1e7, enable_decimal=True)


class _Gate:
    """双向持仓 + 可配置持仓张数的桩。"""
    env = "live"

    def __init__(self, qty: float, side: str = "long"):
        self.qty = float(qty)
        self.side = side
        self.placed: list[dict] = []

    def banner(self):
        return ""

    def get_position_mode(self):
        return "dual"

    def is_dual_position_mode(self):
        return True

    def get_contract(self, symbol):
        return META

    def get_positions(self):
        # 关键：`size` 是 int64（模拟交易所行为，只给整数）
        pos = {
            "contract": SYM,
            "mode": f"dual_{self.side}",
            "size": int(self.qty) if self.side == "long" else -int(self.qty),
            "value": str(abs(self.qty) * 0.01 * 2350.07),
            "entry_price": "2340.5",
            "mark_price": "2350.07",
        }
        return [pos]

    def list_price_orders(self, symbol=None):
        return []

    def list_orders(self, contract=None):
        return []

    def place_price_order(self, body):
        self.placed.append(body)
        return {"id": "po-1", "status": "open"}

    def get_price_order(self, pid):
        last = self.placed[-1] if self.placed else {}
        return {"id": pid, "status": "open",
                "initial": last.get("initial") or {},
                "trigger": last.get("trigger") or {}}

    def cancel_price_order(self, pid):
        return True

    def place_order(self, body):
        self.placed.append(body)
        return {"id": "o-1", "status": "filled"}

    def get_account(self):
        return {"total": "100", "available": "100"}


def _exec(gate) -> Executor:
    import tempfile
    return Executor(gate, root=Path(tempfile.mkdtemp()), label_prefix="vt", bot_id="vt")


def _intent(sl=None, tp=None, side="long", size=None):
    payload = {"action": "modify_tp_sl", "symbol": SYM, "side": side}
    if sl is not None:
        payload["sl"] = sl
    if tp is not None:
        payload["tp"] = tp
    return parse_signal(payload).intents[0]


def _init_bodies(gate) -> list[dict]:
    return [b["initial"] for b in gate.placed if isinstance(b, dict) and "initial" in b]


class TestFractionalWholePosition(unittest.TestCase):
    def test_13_8_contracts_sl_uses_venue_close(self):
        """13.8 张整仓 SL：必须 auto_size（否则截断成 13 张留 0.8 残仓）。"""
        gate = _Gate(qty=13.8)
        ex = _exec(gate)
        step = ex._modify_tp_sl(_intent(sl=2340.7))
        self.assertTrue(step.ok, step.error)
        inits = [i for i in _init_bodies(gate) if str(i.get("text", "")).endswith("-sl")]
        self.assertEqual(len(inits), 1, gate.placed)
        self.assertEqual(float(inits[0]["size"]), 0, "带小数的整仓 SL 必须 size=0")
        self.assertEqual(inits[0]["auto_size"], "close_long")
        self.assertTrue(inits[0]["reduce_only"])

    def test_short_13_8_uses_close_short(self):
        gate = _Gate(qty=13.8, side="short")
        ex = _exec(gate)
        step = ex._modify_tp_sl(_intent(sl=2360.0, side="short"))
        self.assertTrue(step.ok, step.error)
        inits = [i for i in _init_bodies(gate) if str(i.get("text", "")).endswith("-sl")]
        self.assertEqual(inits[0]["auto_size"], "close_short")

    def test_integral_position_keeps_explicit_size(self):
        """整数张（13 张）不能被改变行为：仍然显式 −13。"""
        gate = _Gate(qty=13.0)
        ex = _exec(gate)
        step = ex._modify_tp_sl(_intent(sl=2340.7))
        self.assertTrue(step.ok, step.error)
        inits = [i for i in _init_bodies(gate) if str(i.get("text", "")).endswith("-sl")]
        self.assertEqual(float(inits[0]["size"]), -13, "整数张应保持显式张数")
        self.assertFalse(inits[0].get("auto_size"))

    def test_partial_tier_not_promoted_to_whole_side(self):
        """部分档（2.3 张 < 持仓 13.8）绝不能被放大成整侧平仓。"""
        gate = _Gate(qty=13.8)
        ex = _exec(gate)
        it = _intent(tp=2361.0)
        it.trigger_rule_tp = infer_trigger_rules("open_long", True)
        it.trigger_rule_sl = infer_trigger_rules("open_long", False)
        ex._place_trigger(it, "short", 2361.0, True, META, 2.3)
        inits = _init_bodies(gate)
        self.assertEqual(len(inits), 1, gate.placed)
        self.assertFalse(inits[0].get("auto_size"),
                         "部分档不该走 auto_size（会平掉整侧）")
        self.assertNotEqual(float(inits[0]["size"]), 0)

    def test_tiny_residual_still_venue_close(self):
        """<1 张残仓仍走 auto_size（既有行为不回退）。"""
        gate = _Gate(qty=0.4)
        ex = _exec(gate)
        step = ex._modify_tp_sl(_intent(sl=2340.7))
        self.assertTrue(step.ok, step.error)
        inits = [i for i in _init_bodies(gate) if str(i.get("text", "")).endswith("-sl")]
        self.assertEqual(inits[0]["auto_size"], "close_long")


class TestConversionChain(unittest.TestCase):
    """USDT ↔ 张数换算逐层核对（真实订单口径）。"""

    def test_usd_to_contracts_eth_13_8(self):
        from omnialpha.sizing import usd_to_contracts
        q = usd_to_contracts(323.0, 2340.5, META)
        self.assertAlmostEqual(q, 13.8, places=6)
        # 真实持仓 0.138 ETH
        self.assertAlmostEqual(q * 0.01, 0.138, places=6)

    def test_position_qty_reads_value_not_size(self):
        from omnialpha.gate_client import position_qty
        pos = {"contract": SYM, "size": 13, "value": "324.30966",
               "mark_price": "2350.07"}
        self.assertAlmostEqual(position_qty(pos, META), 13.8, places=4)

    def test_int_price_order_truncates(self):
        """证明「为什么必须 auto_size」：价格触发单只接受整数张。"""
        from omnialpha.gate_client import _int_price_order_size
        body = {"initial": {"size": -13.8, "text": "t-sl", "close": False}}
        out = _int_price_order_size(body)
        self.assertEqual(float(out["initial"]["size"]), -13)


if __name__ == "__main__":
    unittest.main()
