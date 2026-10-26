# -*- coding: utf-8 -*-
"""小数张读取侧回归（服务器线，2026-10-26）。

线上这条线（`server-master-20261026`）原先在**读**持仓/保护单时用 `int(size)`：
Gate 对小数张合约（ETH `order_size_min=0.1`）返回字符串 `"-0.7"` → `int()` 抛
`ValueError`；若给的是 float `0.7` → 截成 0 → 被判成「空仓 / 没有保护单」。
0.7 张**真正成交那一轮**就会踩到，所以用测试钉住。

本文件只覆盖读取侧（下单尺寸解析是另一件事，未在本分支改动）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor, _num, _parse_symbol_positions, qty
from omnialpha.gate_client import ContractMeta

SYM = "ETH_USDT"
PX = 2325.0
QM = 0.01          # 1 张 = 23.25U


class FakeGate:
    """最小替身：持仓 / 条件单 / 价格。"""

    env = "live"
    equity = 100.0

    def __init__(self):
        self.positions: list[dict] = []
        self.price_orders: dict[str, dict] = {}
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self._seq = 0

    def banner(self):
        return "[FAKE]"

    def get_contract(self, symbol):
        # ETH 是小数张合约：size_min=0.1 / enable_decimal=True → size_step=0.1
        return ContractMeta(name=symbol, quanto_multiplier=QM, order_size_round=1,
                            order_price_round=0.01, leverage_max=100,
                            size_min=0.1, size_max=1e7, enable_decimal=True)

    def get_last_price(self, symbol):
        return PX

    def get_ticker(self, symbol):
        return {"mark_price": PX}

    def get_account(self):
        return {"total": str(self.equity), "balance": str(self.equity)}

    def get_available_usdt(self):
        return self.equity

    def get_positions(self):
        return [dict(p) for p in self.positions]

    def get_position_mode(self):
        return "single"

    def set_leverage(self, *a, **k):
        return True

    def place_order(self, body):
        self._seq += 1
        self.placed.append(dict(body))
        return {"id": str(self._seq), "status": "open", "size": body.get("size")}

    def place_price_order(self, body):
        self._seq += 1
        oid = str(self._seq)
        init = dict(body.get("initial") or {})
        self.price_orders[oid] = {"id": oid, "status": "open", "initial": init,
                                  "trigger": dict(body.get("trigger") or {}),
                                  "text": init.get("text"),
                                  "contract": init.get("contract")}
        self.placed.append(dict(body))
        return dict(self.price_orders[oid])

    def list_orders(self, symbol=None):
        return []

    def list_price_orders(self, symbol=None):
        return [dict(o) for o in self.price_orders.values()]

    def get_order(self, oid):
        return {}

    def get_price_order(self, oid):
        return dict(self.price_orders.get(str(oid)) or {})

    def cancel_order(self, oid):
        self.cancelled.append(str(oid))
        return True

    def cancel_price_order(self, oid):
        self.cancelled.append(str(oid))
        self.price_orders.pop(str(oid), None)
        return True

    def cancel_all_orders(self, symbol=None):
        return {}

    def cancel_all_price_orders(self, symbol=None):
        return {}

    def set_position(self, size, raw=None):
        """`raw` 用来构造 Gate 真实返回的字符串形态（`"-0.7"`）。"""
        self.positions = ([{"contract": SYM, "size": raw if raw is not None else size,
                            "entry_price": PX, "mark_price": PX}]
                          if size != 0 else [])

    def add_price_order(self, text, size, trigger_price, rule=None):
        self._seq += 1
        oid = str(self._seq)
        self.price_orders[oid] = {
            "id": oid, "status": "open", "text": text, "contract": SYM,
            "initial": {"contract": SYM, "size": size, "text": text,
                        "is_reduce_only": True, "price": "0", "tif": "ioc"},
            "trigger": {"price": str(trigger_price),
                        "rule": rule if rule is not None else (2 if size < 0 else 1)},
        }
        return oid


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.gate = FakeGate()
        self.ex = Executor(self.gate, root=self.root, label_prefix="pt", bot_id="frac")

    def tearDown(self):
        self._td.cleanup()


class TestHelpers(unittest.TestCase):
    def test_num_handles_string_float_none(self):
        self.assertAlmostEqual(_num("-0.7"), -0.7)
        self.assertAlmostEqual(_num("0.7"), 0.7)
        self.assertEqual(_num(None), 0.0)
        self.assertEqual(_num(""), 0.0)
        self.assertEqual(_num("abc"), 0.0)
        self.assertEqual(_num(True), 0.0)
        self.assertAlmostEqual(qty("-0.7"), 0.7)


class TestParsePositions(Base):
    def test_string_fractional_size_does_not_raise(self):
        pos = _parse_symbol_positions(
            [{"contract": SYM, "size": "-0.7", "entry_price": "2300"}], SYM)
        self.assertEqual(len(pos), 1)
        self.assertAlmostEqual(pos[0]["size"], -0.7)
        self.assertEqual(pos[0]["side"], "short")

    def test_float_fractional_size_kept(self):
        pos = _parse_symbol_positions(
            [{"contract": SYM, "size": 0.7, "entry_price": "2300"}], SYM)
        self.assertEqual(len(pos), 1, "0.7 张持仓不许被当成空仓")
        self.assertAlmostEqual(pos[0]["size"], 0.7)
        self.assertEqual(pos[0]["side"], "long")

    def test_symbol_positions_reader_keeps_fraction(self):
        self.gate.set_position(0.7, raw="-0.7")
        pos = self.ex._symbol_positions(SYM)
        self.assertEqual(len(pos), 1)
        self.assertAlmostEqual(abs(pos[0]["size"]), 0.7)


class TestProtectors(Base):
    def test_sl_size_sums_fraction(self):
        self.gate.add_price_order("t-pt-signal-sl", -0.7, 2280)
        self.gate.add_price_order("t-pt-l2-sl", -0.7, 2280)
        self.assertAlmostEqual(self.ex._owned_sl_size(SYM, self.ex._own_prefix("")), 1.4)

    def test_sl_orders_keep_side_and_size(self):
        self.gate.add_price_order("t-pt-signal-sl", -0.7, 2280)   # 平多 → long
        self.gate.add_price_order("t-pt-l2-sl", 0.7, 2400)        # 平空 → short
        rows = self.ex._owned_sl_orders_sided(SYM)
        self.assertEqual(len(rows), 2)
        by_side = {side: (px, sz) for _pid, px, sz, side in rows}
        self.assertAlmostEqual(by_side["long"][1], 0.7)
        self.assertAlmostEqual(by_side["short"][1], 0.7)

    def test_ensure_protection_sees_sl_present(self):
        """已有 0.7 张 SL 且持仓 0.7 张 → 不许再补（原来 0.7 被读成 0 → 判成裸仓）。"""
        ex = Executor(self.gate, root=self.root, label_prefix="pt", bot_id="frac",
                      account_risk={"auto_protect": True, "auto_protect_sl_pct": 2.0})
        self.gate.set_position(0.7)
        self.gate.add_price_order("t-pt-signal-sl", -0.7, 2280)
        before = set(self.gate.price_orders)
        out = ex.ensure_protection(SYM)
        self.assertEqual(out.get("skipped"), "sl_present", out)
        self.assertEqual(set(self.gate.price_orders), before)
        self.assertEqual(self.gate.cancelled, [], "已覆盖的持仓不许重挂/撤单")


class TestOtherReaders(Base):
    def test_open_symbols_sees_fractional_position(self):
        self.gate.set_position(0.7)
        self.assertEqual(self.ex._open_symbols(), [SYM])

    def test_give_back_size_not_truncated(self):
        self.gate.set_position(-0.7)
        out = self.ex._give_back_side(
            {"side": "short", "size": -0.7, "entry_price": PX}, SYM, {}, dry=True)
        self.assertAlmostEqual(out["position_size"], 0.7)


class TestAlignTpGroup(Base):
    """减仓后 TP 腿对齐（`_align_tp_group` / `_apportion_sizes`）。

    原逻辑全按整数张算：`_apportion_sizes(0.7, [1])` 会因 `diff=0.7` 给这条腿补 1 张
    → 挂出 **1 张 reduce_only** 而持仓只有 0.7 张 → 交易所直接拒（超平）。
    """

    def _leg(self, gate, sz, px="2400"):
        oid = gate.add_price_order("t-pt-signal-tp", sz, px, rule=1)
        return {"psz": sz, "pid": oid, "text": "t-pt-signal-tp",
                "rule": 1, "price_type": 0, "px": px}

    def test_fractional_total_not_rounded_up(self):
        self.gate.set_position(0.7)
        leg = self._leg(self.gate, -0.4)
        notes = self.ex._align_tp_group(SYM, [leg], 0.7)
        sizes = [abs(float(o["initial"]["size"]))
                 for o in self.gate.price_orders.values()]
        self.assertEqual(len(sizes), 1, f"应只剩一条腿：{notes}")
        self.assertAlmostEqual(sum(sizes), 0.7, msg=f"合计必须等于持仓 0.7：{notes}")

    def test_aligned_fraction_is_zero_churn(self):
        self.gate.set_position(0.7)
        leg = self._leg(self.gate, -0.7)
        before = set(self.gate.price_orders)
        notes = self.ex._align_tp_group(SYM, [leg], 0.7)
        self.assertEqual(notes, [], "已对齐就不该动")
        self.assertEqual(set(self.gate.price_orders), before)

    def test_integer_total_still_integral(self):
        """整数张（BTC 步长 1）行为不变：6 张分 3 腿 → 2/2/2。"""
        self.gate.set_position(6)
        legs = [self._leg(self.gate, -3, px) for px in ("2400", "2450", "2500")]
        self.ex._align_tp_group(SYM, legs, 6)
        sizes = sorted(abs(float(o["initial"]["size"]))
                       for o in self.gate.price_orders.values())
        self.assertEqual(sizes, [2.0, 2.0, 2.0])


class TestGateClientFlatten(unittest.TestCase):
    def _client(self):
        from omnialpha.gate_client import GateClient

        class C(GateClient):
            def __init__(self):
                super().__init__(api_key="", api_secret="", env="live")
                self.sent = []

            def is_dual_position_mode(self):
                return True

            def get_positions(self):
                return [{"contract": SYM, "size": "-0.7", "mode": "dual_long",
                         "entry_price": str(PX)}]

            def place_order(self, body):
                self.sent.append(dict(body))
                return {"id": "1", "status": "open"}

        return C()

    def test_pos_is_side_accepts_fractional_string(self):
        from omnialpha.gate_client import _infer_side, _pos_is_side
        self.assertTrue(_pos_is_side({"size": "0.7"}, "long"))
        self.assertFalse(_pos_is_side({"size": "0.7"}, "short"))
        self.assertEqual(_infer_side({"size": "-0.7"}), "short")

    def test_close_position_sends_fractional_size(self):
        c = self._client()
        c.close_position(SYM, side="long")
        self.assertEqual(len(c.sent), 1)
        self.assertAlmostEqual(float(c.sent[0]["size"]), -0.7)
        self.assertTrue(c.sent[0].get("reduce_only"))


if __name__ == "__main__":
    unittest.main()
