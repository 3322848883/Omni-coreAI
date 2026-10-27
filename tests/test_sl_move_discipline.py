# -*- coding: utf-8 -*-
"""SL 移动纪律（`_sl_move_rejected`）：区分「移动既有 SL」与「从无到有补保护」。

2026-10-27 08:32 实盘回归：pa-eth-v5 有一条 0.1 张裸空腿（`old_sl == 0`，
该方向压根没有 SL）。AI 要求挂硬 SL 2324.72，被判
`not floating (mark 2312.94 vs entry 2311.05); SL move only allowed in profit`
→ 空腿继续无保护，且 `_modify_tp_sl` 把它报成 `ok:true` 的良性 no-op（静默丢腿）。

纪律的正确边界：
  * `old_sl > 0`（有旧 SL）→ 浮盈才许动、只许收紧、不许同值 churn；
  * `old_sl == 0`（裸仓）→ 这是「补保护」不是「放宽」，只保留即时触发护栏；
  * `ladder/grid` 策略 → 跳过浮盈 gate 且允许放宽，只拦同值 churn。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor, _sl_move_rejected  # noqa: E402

SYM = "ETH_USDT"
ENTRY_SHORT = 2311.05
MARK_LOSS_SHORT = 2312.94      # 空单浮亏
MARK_PROFIT_SHORT = 2300.0     # 空单浮盈


class TestNoExistingSl(unittest.TestCase):
    """`old_sl == 0`：裸仓补保护，不能被浮盈纪律挡。"""

    def test_naked_short_loss_allows_sl(self):
        self.assertEqual(
            _sl_move_rejected("short", ENTRY_SHORT, 2324.72,
                              old_sl=0.0, mark=MARK_LOSS_SHORT), "",
            "裸空腿（无旧 SL）浮亏时也允许挂硬 SL")

    def test_naked_long_loss_allows_sl(self):
        # mark 2305 < entry 2310.8（多单浮亏），SL 2297.47 < mark（不即时触发）
        self.assertEqual(
            _sl_move_rejected("long", 2310.8, 2297.47,
                              old_sl=0.0, mark=2305.0), "")

    def test_naked_leg_still_blocks_instant_trigger_short(self):
        """护栏不许一起放开：空单 SL 挂到 mark 下方会挂上即平。"""
        self.assertIn("instantly",
                      _sl_move_rejected("short", ENTRY_SHORT, 2310.0,
                                        old_sl=0.0, mark=2312.94))

    def test_naked_leg_still_blocks_instant_trigger_long(self):
        self.assertIn("instantly",
                      _sl_move_rejected("long", 2310.8, 2330.0,
                                        old_sl=0.0, mark=2312.94))


class TestExistingSl(unittest.TestCase):
    """`old_sl > 0`：原有纪律一条不少。"""

    def test_existing_sl_short_loss_rejected(self):
        reason = _sl_move_rejected("short", ENTRY_SHORT, 2324.72,
                                   old_sl=2320.0, mark=MARK_LOSS_SHORT)
        self.assertIn("not floating", reason)

    def test_existing_sl_long_profit_tighten_allowed(self):
        self.assertEqual(
            _sl_move_rejected("long", 2310.8, 2305.0,
                              old_sl=2300.0, mark=2315.0), "")

    def test_existing_sl_long_profit_widen_rejected(self):
        reason = _sl_move_rejected("long", 2310.8, 2290.0,
                                   old_sl=2300.0, mark=2315.0)
        self.assertIn("widen", reason)

    def test_existing_sl_short_profit_tighten_allowed(self):
        self.assertEqual(
            _sl_move_rejected("short", ENTRY_SHORT, 2305.0,
                              old_sl=2310.0, mark=MARK_PROFIT_SHORT), "")

    def test_existing_sl_same_value_blocked(self):
        reason = _sl_move_rejected("short", ENTRY_SHORT, 2305.0,
                                   old_sl=2305.0, mark=MARK_PROFIT_SHORT)
        self.assertTrue(reason, "同值必须拦（churn）")


class TestLadderPolicy(unittest.TestCase):
    def test_ladder_allows_widen_in_loss(self):
        self.assertEqual(
            _sl_move_rejected("long", 2352.0, 2330.0,
                              old_sl=2340.0, mark=2345.0, policy="ladder"), "")

    def test_ladder_still_blocks_churn(self):
        reason = _sl_move_rejected("long", 2352.0, 2340.0,
                                   old_sl=2340.0, mark=2345.0, policy="ladder")
        self.assertIn("no-op", reason)


class TestModifyPlacesProtectionOnNakedLeg(unittest.TestCase):
    """端到端：0.1 张裸空腿的 `modify_tp_sl(side=short)` 必须真的挂出单。

    旧行为：ok:true + detail.noop（什么都没挂）—— 最危险的一种「成功」。
    """

    class _Gate:
        env = "live"

        def __init__(self):
            self.placed = []
            self.pos = {"contract": SYM, "mode": "dual_short", "size": 0,
                        "entry_price": str(ENTRY_SHORT),
                        "mark_price": str(MARK_LOSS_SHORT),
                        "value": "2.3178"}

        def banner(self):
            return ""

        def get_position_mode(self):
            return "dual"

        def is_dual_position_mode(self):
            return True

        def get_contract(self, symbol):
            from omnialpha.gate_client import ContractMeta
            return ContractMeta(name=SYM, quanto_multiplier=0.01,
                                order_size_round=1, order_price_round=0.01,
                                leverage_max=100, size_min=0.1, size_max=1e7,
                                enable_decimal=True)

        def get_positions(self):
            return [dict(self.pos)]

        def list_price_orders(self, symbol=None):
            return []

        def list_orders(self, contract=None):
            return []

        def place_price_order(self, body):
            self.placed.append(body)
            self.last = {"id": "po-1", "status": "open",
                         "initial": body.get("initial") or {},
                         "trigger": body.get("trigger") or {}}
            return {"id": "po-1", "status": "open"}

        def get_price_order(self, pid):
            return dict(getattr(self, "last", {"id": pid, "status": "open"}))

        def cancel_price_order(self, pid):
            return True

        def place_order(self, body):
            self.placed.append(body)
            return {"id": "o-1", "status": "filled"}

        def get_account(self):
            return {"total": "100", "available": "100"}

    def test_naked_short_leg_gets_venue_sl(self):
        from omnialpha.schema import parse_signal
        gate = self._Gate()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ex = Executor(gate, root=Path(td.name), label_prefix="vt", bot_id="vt")
        intent = parse_signal({
            "action": "modify_tp_sl", "symbol": SYM, "side": "short",
            "sl": 2324.72,
        }).intents[0]
        step = ex._modify_tp_sl(intent)
        self.assertTrue(step.ok, step.error)
        detail = step.detail or {}
        self.assertNotIn("noop", detail, f"不许静默 no-op：{detail}")
        self.assertTrue(detail.get("sl_placed"), detail)
        bodies = [b for b in gate.placed if "initial" in b]
        self.assertEqual(len(bodies), 1, gate.placed)
        init = bodies[0]["initial"]
        self.assertEqual(init["size"], 0, "0.1 张残仓必须走 venue-close")
        self.assertEqual(init["auto_size"], "close_short")


if __name__ == "__main__":
    unittest.main()
