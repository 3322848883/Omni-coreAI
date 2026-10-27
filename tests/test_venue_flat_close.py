# -*- coding: utf-8 -*-
"""venue-computed close：**全平不下张数，减仓才下张数**。

修的是 2026-10-27 实盘病：`/positions` 的 `size` 是 int64，ETH 这种
`enable_decimal` 合约会被截断（真实 16.1 张 → 回报 -16，真实 0.1 张 → 回报 0）。
`close_position` 过去拿 `size` 求和当平仓量 → 每轮少平 0.1~0.9 张；reduce_only
只减不增 ⇒ 残量永久化，双向模式下同时留着 0.1 张多仓和 0.1 张空仓
（`position_close` 从 10-26 21:21 之后再无记录 —— 两边都没回过 0）。

本文件钉住四件事：
  1. 全平走 `size=0 + auto_size`（dual）/ `close=true`（single），**不带张数**；
  2. 真减仓才带张数，且请求量 ≥ 真实持仓时升级成全平；
  3. 读侧一律用 `position_qty`（`value/(mark×quanto)`）——幽灵仓不再隐身；
  4. venue 保护腿（`size=0`）的方向靠 `auto_size`，不能被孤儿扫描误撤、
     也不能在 resync / TP 分摊里造成 churn。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import (  # noqa: E402
    Executor,
    _parse_symbol_positions,
    _protector_side,
)
from omnialpha.gate_client import (  # noqa: E402
    ContractMeta,
    _int_price_order_size,
    position_qty,
)
from omnialpha.strategist.snapshot import positions_view  # noqa: E402

SYM = "ETH_USDT"


def _meta(quanto=0.01, step=0.1) -> ContractMeta:
    return ContractMeta(name=SYM, quanto_multiplier=quanto, order_size_round=1,
                        order_price_round=0.01, leverage_max=100,
                        size_min=step, size_max=1e7, enable_decimal=(step < 1))


# ── 交易所原始行的两种形态：截断前 / 截断后 ──────────────────────────
GHOST_LONG = {"contract": SYM, "mode": "dual_long", "size": 0,
              "entry_price": "2310.8", "mark_price": "2300", "value": "2.3"}
REAL_SHORT = {"contract": SYM, "mode": "dual_short", "size": -16,
              "entry_price": "2311.05", "mark_price": "2300", "value": "370.3"}


class TestPositionQty(unittest.TestCase):
    def test_ghost_long_recovered_from_value(self):
        """`size=0` + `value≠0` = 0.1 张多仓（旧的 `if size == 0: continue` 会吞掉）。"""
        self.assertAlmostEqual(position_qty(GHOST_LONG, _meta()), 0.1, places=9)

    def test_truncated_short_is_16_1(self):
        self.assertAlmostEqual(position_qty(REAL_SHORT, _meta()), -16.1, places=9)

    def test_without_meta_falls_back_to_size(self):
        """元数据缺失 → 退回 size 字段（旧行为，不是抛异常）。"""
        self.assertEqual(position_qty(REAL_SHORT, None), -16)
        self.assertEqual(position_qty(GHOST_LONG, None), 0)


class TestParseKeepsGhost(unittest.TestCase):
    def test_ghost_survives_with_meta(self):
        rows = _parse_symbol_positions([GHOST_LONG, REAL_SHORT], SYM, meta=_meta())
        sides = {r["side"]: r["size"] for r in rows}
        self.assertIn("long", sides, "0.1 张多仓不许被当成空仓丢掉")
        self.assertAlmostEqual(sides["long"], 0.1, places=9)
        self.assertAlmostEqual(sides["short"], -16.1, places=9)

    def test_ghost_dropped_without_meta(self):
        """没有元数据时保持旧行为（size=0 → 丢弃），确认分支真的存在。"""
        rows = _parse_symbol_positions([GHOST_LONG], SYM)
        self.assertEqual(rows, [])

    def test_size_field_exposed_for_gap_audit(self):
        row = _parse_symbol_positions([REAL_SHORT], SYM, meta=_meta())[0]
        self.assertAlmostEqual(row["size"], -16.1, places=9)
        self.assertEqual(row["size_field"], -16.0)


class TestSnapshotSeesGhost(unittest.TestCase):
    def test_positions_view_includes_fractional_ghost(self):
        class C:
            def get_positions(self):
                return [dict(GHOST_LONG), dict(REAL_SHORT)]

            def get_contract(self, symbol):
                return _meta()

        rows = positions_view(C())
        sizes = {r["mode"]: r["size"] for r in rows}
        self.assertEqual(len(rows), 2, f"AI 必须看到两条腿：{rows}")
        self.assertAlmostEqual(sizes["dual_long"], 0.1, places=9)
        self.assertAlmostEqual(sizes["dual_short"], -16.1, places=9)

    def test_zero_position_still_filtered(self):
        class C:
            def get_positions(self):
                return [{"contract": SYM, "mode": "dual_long", "size": 0,
                         "value": "0", "mark_price": "2304.13"}]

            def get_contract(self, symbol):
                return _meta()

        self.assertEqual(positions_view(C()), [])


class TestPriceOrderIntGate(unittest.TestCase):
    def test_auto_size_with_size_zero_passes(self):
        """venue 保护单 size 必须是 0 —— 不许被「不足 1 张」拦掉。"""
        body = {"initial": {"contract": SYM, "size": 0, "reduce_only": True,
                            "auto_size": "close_long", "price": "0", "tif": "ioc"}}
        self.assertIs(_int_price_order_size(body), body)

    def test_fractional_without_auto_size_still_floored(self):
        body = {"initial": {"contract": SYM, "size": -16.1, "price": "0", "tif": "ioc"}}
        self.assertEqual(_int_price_order_size(body)["initial"]["size"], -16)

    def test_below_one_contract_still_raises(self):
        body = {"initial": {"contract": SYM, "size": 0.4, "price": "0", "tif": "ioc"}}
        with self.assertRaises(Exception):
            _int_price_order_size(body)


class TestProtectorSide(unittest.TestCase):
    def test_auto_size_wins(self):
        self.assertEqual(_protector_side({"size": 0, "auto_size": "close_long"}), "long")
        self.assertEqual(_protector_side({"size": 0, "auto_size": "close_short"}), "short")

    def test_sign_fallback(self):
        self.assertEqual(_protector_side({"size": -7}), "long")
        self.assertEqual(_protector_side({"size": 7}), "short")
        self.assertIsNone(_protector_side({"size": 0}))


class _Gate:
    """最小交易所替身：真实张数（value 反算）+ venue 保护腿。"""

    env = "live"

    def __init__(self, positions, price_orders=None):
        self.positions = positions
        self.price_orders = list(price_orders or [])
        self.placed = []
        self.cancelled = []

    def banner(self):
        return ""

    def get_position_mode(self):
        return "dual"

    def is_dual_position_mode(self):
        return True

    def get_contract(self, symbol):
        return _meta()

    def get_positions(self):
        return [dict(p) for p in self.positions]

    def list_price_orders(self, symbol=None):
        return [dict(o) for o in self.price_orders]

    def place_price_order(self, body):
        self.placed.append(body)
        return {"id": "new", "status": "open"}

    def cancel_price_order(self, pid):
        self.cancelled.append(str(pid))
        return True

    def list_orders(self, contract=None):
        return []

    def place_order(self, body):
        self.placed.append(body)
        return {"id": "o", "status": "filled"}

    def get_account(self):
        return {"total": "100", "available": "100"}


def _venue_leg(pid, side, text, trigger, auto=True, size=0):
    return {
        "id": pid, "status": "open", "contract": SYM,
        "initial": {"contract": SYM, "size": size, "reduce_only": True,
                    "text": text, "price": "0", "tif": "ioc",
                    **({"auto_size": f"close_{side}"} if auto else {})},
        "trigger": {"price": str(trigger),
                    "rule": 2 if side == "long" else 1, "price_type": 1},
    }


class TestVenueProtectorNotOrphan(unittest.TestCase):
    def _ex(self, gate, risk=None):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        return Executor(gate, root=Path(self._td.name), label_prefix="vt",
                        bot_id="vt", account_risk=risk or {})

    def test_venue_sl_matching_position_is_not_orphan(self):
        """`size=0` 的 SL 靠 auto_size 定方向 —— 不许被判成孤儿撤掉（=裸仓）。"""
        gate = _Gate([REAL_SHORT], [_venue_leg("1", "short", "t-vt-sl", 2318)])
        ex = self._ex(gate)
        po = gate.price_orders[0]
        self.assertFalse(ex._is_orphan_protector(po, ex._symbol_positions(SYM)))

    def test_venue_sl_without_position_is_orphan(self):
        gate = _Gate([], [_venue_leg("1", "short", "t-vt-sl", 2318)])
        ex = self._ex(gate)
        self.assertTrue(ex._is_orphan_protector(gate.price_orders[0],
                                                ex._symbol_positions(SYM)))

    def test_coverage_materialized_for_venue_leg(self):
        """venue 腿张数物化成该方向真实持仓（16.1）→ 下游「合计 vs 持仓」不失真。"""
        gate = _Gate([REAL_SHORT], [_venue_leg("1", "short", "t-vt-sl", 2318)])
        ex = self._ex(gate)
        rows = ex._owned_sl_orders_sided(SYM)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][3], "short")
        self.assertAlmostEqual(rows[0][2], 16.1, places=9)

    def test_resync_no_churn_for_aligned_venue_legs(self):
        """SL/TP 都是 venue 腿且与持仓一致 → 一张不动（零 churn）。"""
        gate = _Gate([REAL_SHORT], [
            _venue_leg("1", "short", "t-vt-sl", 2318),
            _venue_leg("2", "short", "t-vt-tp", 2290),
        ])
        ex = self._ex(gate, risk={"use_venue_close": True})
        notes = ex._resync_protectors(SYM, label="")
        self.assertEqual(gate.placed, [], f"不该重挂：{notes}")
        self.assertEqual(gate.cancelled, [], f"不该撤单：{notes}")

    def test_venue_sl_merge_keeps_venue_semantics(self):
        """两条同价 venue SL 归并成一条 → 仍必须是 size=0 + auto_size（不带张数）。"""
        gate = _Gate([REAL_SHORT], [
            _venue_leg("1", "short", "t-vt-sl", 2318),
            _venue_leg("2", "short", "t-vt-sl", 2318),
        ])
        ex = self._ex(gate, risk={"use_venue_close": True})
        ex._resync_protectors(SYM, label="")
        self.assertEqual(len(gate.placed), 1, gate.placed)
        init = gate.placed[0]["initial"]
        self.assertEqual(init["size"], 0)
        self.assertEqual(init["auto_size"], "close_short")
        self.assertNotIn("close", init)


class TestFractionalResidualProtection(unittest.TestCase):
    """0.1 张残仓的 SL 必须自动走 venue-close，不能被整数闸门拒成整轮失败。

    实盘 2026-10-27 15:26（北京）复现：SL 触发平 16 张后剩 0.1 张空仓，
    下一轮 AI 正确地要求「给两侧补 SL」，执行却报
    `sl#2 price_orders 只接受整数张：-0.1 张不足 1 张` → 整轮 ok:false、
    残仓继续裸奔。残仓的「全平该方向」在语义上就等于原意，所以张数 <1 时
    必须无条件走 `auto_size`（不依赖 `use_venue_close` 配置）。
    """

    def _ex(self, gate, risk=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Executor(gate, root=Path(td.name), label_prefix="vt",
                        bot_id="vt", account_risk=risk or {})

    def _intent(self, side):
        from omnialpha.schema import parse_signal
        return parse_signal({
            "action": "open_long" if side == "long" else "open_short",
            "symbol": SYM, "size_usd": 100, "price": 2300,
            "sl": 2290 if side == "long" else 2310,
            "tp": 2320 if side == "long" else 2280,
            "side": side,
        }).intents[0]

    def test_fractional_sl_uses_venue_close_without_config(self):
        """配置里没有 use_venue_close，0.1 张 SL 也必须挂成 auto_size（size=0）。"""
        gate = _Gate([GHOST_LONG])
        ex = self._ex(gate)  # account_risk={}
        ex._place_trigger(self._intent("long"), "long", 2299.4, False,
                          _meta(), 0.1)
        self.assertEqual(len(gate.placed), 1, gate.placed)
        init = gate.placed[0]["initial"]
        self.assertEqual(init["size"], 0, "残仓保护单不能带张数")
        self.assertEqual(init["auto_size"], "close_long")
        self.assertTrue(init["reduce_only"])
        self.assertNotIn("close", init)

    def test_fractional_short_sl_uses_close_short(self):
        gate = _Gate([{"contract": SYM, "mode": "dual_short", "size": 0,
                       "entry_price": "2311.05", "mark_price": "2300",
                       "value": "2.3"}])
        ex = self._ex(gate)
        ex._place_trigger(self._intent("short"), "short", 2323.1, False,
                          _meta(), 0.1)
        init = gate.placed[0]["initial"]
        self.assertEqual(init["size"], 0)
        self.assertEqual(init["auto_size"], "close_short")

    def test_full_size_sl_keeps_explicit_size(self):
        """张数 ≥1 时保持显式张数（网格/阶梯多档 TP 不能被 auto_size 吞掉）。"""
        gate = _Gate([REAL_SHORT])
        ex = self._ex(gate)
        ex._place_trigger(self._intent("short"), "short", 2323.1, False,
                          _meta(), 16)
        init = gate.placed[0]["initial"]
        self.assertEqual(init["size"], 16)
        self.assertNotIn("auto_size", init)

    def test_ensure_protection_no_longer_skips_fractional(self):
        """auto_protect 不许再把 <1 张的裸仓标成 skip 后放走。"""
        gate = _Gate([GHOST_LONG])
        gate.get_ticker = lambda s: {"mark_price": "2300"}
        ex = self._ex(gate, risk={"auto_protect": True, "auto_protect_sl_pct": 1.0})
        out = ex.ensure_protection(SYM)
        self.assertNotEqual(out.get("skipped"), "below_min_protectable", out)
        self.assertEqual(len(gate.placed), 1, out)
        self.assertEqual(gate.placed[0]["initial"]["auto_size"], "close_long")


class TestAlignTpSkipsVenue(unittest.TestCase):
    def test_venue_tp_not_apportioned(self):
        """venue TP 没有可分摊张数 —— 混进 `_align_tp_group` 会把它改成显式张数。"""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        gate = _Gate([REAL_SHORT], [_venue_leg("2", "short", "t-vt-tp", 2290)])
        ex = Executor(gate, root=Path(td.name), label_prefix="vt", bot_id="vt")
        legs = [{"pid": "2", "psz": 0.0, "px": "2290", "rule": 1, "price_type": 1,
                 "text": "t-vt-tp", "auto_size": "close_short"}]
        notes = ex._align_tp_group(SYM, legs, 16.1)
        self.assertEqual(notes, [])
        self.assertEqual(gate.placed, [], "venue 腿不许被重新分摊")


class _Feed:
    """纸面行情源：固定价、固定规格，不联网。"""

    name = "fake"

    def __init__(self, px=2340.0):
        self.px = px
        self.meta = _meta()
        self.funding_rate = 0.0001

    def get_last_price(self, symbol):
        return self.px

    def get_orderbook_top(self, symbol, limit=5):
        return {"bids": [{"p": self.px, "s": 1e9}],
                "asks": [{"p": self.px, "s": 1e9}]}

    def get_ticker(self, symbol):
        return {"last": self.px, "mark_price": self.px,
                "funding_rate": self.funding_rate}

    def get_contract(self, symbol):
        return self.meta

    def get_klines(self, symbol, interval, limit=100):
        return []

    def get_contract_stats(self, symbol, limit=1, interval=""):
        return []


class TestPaperVenueClose(unittest.TestCase):
    def setUp(self):
        from omnialpha.paper.exchange import PaperExchange

        self._td = tempfile.TemporaryDirectory()
        tmp = Path(self._td.name)
        self.ex = PaperExchange(
            env="paper",
            store_path=tmp / "data" / "bots" / "vt" / "paper" / "account.db",
            feed=_Feed(),
            config={"initial_capital": 100000, "leverage": 20},
        )

    def tearDown(self):
        self.ex.store.close()
        self._td.cleanup()

    def _open(self, size):
        o = self.ex.place_order({"contract": SYM, "size": size, "type": "market"})
        self.assertIn(str(o.get("status")), ("filled", "finished"), o)

    def _pos(self):
        return sum(float(p.get("size") or 0) for p in self.ex.get_positions())

    def test_venue_close_flattens_fractional_exactly(self):
        """0.7 张（小数）用 `auto_size` 全平 → 归零，不留残仓。"""
        self._open(0.7)
        self.assertAlmostEqual(self._pos(), 0.7, places=9)
        self.ex.place_order({"contract": SYM, "size": 0, "auto_size": "close_long",
                             "price": "0", "tif": "ioc", "reduce_only": True})
        self.assertEqual(self._pos(), 0.0, "venue close 必须平到 0")

    def test_close_position_size_zero_flattens(self):
        self._open(-0.7)
        self.ex.close_position(SYM, side="short")
        self.assertEqual(self._pos(), 0.0)

    def test_venue_price_order_resolves_signed_size(self):
        self._open(-0.7)
        po = self.ex.place_price_order({
            "initial": {"contract": SYM, "size": 0, "reduce_only": True,
                        "auto_size": "close_short", "price": "0", "tif": "ioc",
                        "text": "t-vt-sl"},
            "trigger": {"rule": 1, "price_type": 1, "price": "2400"},
        })
        self.assertAlmostEqual(float(po["size"]), 0.7, places=9,
                               msg="平空 → 买入正张数")


if __name__ == "__main__":
    unittest.main()
