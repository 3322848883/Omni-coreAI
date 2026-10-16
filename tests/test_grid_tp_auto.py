# -*- coding: utf-8 -*-
"""阶梯档止盈执行器：单向模式「普通限价卖单 + reduce_only」形态。

**事故与定稿（2026-10-16）**：曾经采用「普通限价卖单（无 reduce_only）+ 无持仓预挂」
以追求「完全挂完」——实盘证明**不安全**：单向模式下卖单是方向性订单，无多头成交即开空，
2026-10-15 22:51~10-16 05:01 累积成 **−14 张空单**，长仓人格随后被 `no_flip` 拦死
（连续 12 轮 "持有 short 时不能直接开 long"），网格停摆 4 小时。

**定稿语义（本文件锁定）**：
  - ⛔ **硬闸**：`多头持仓 <= 0` → **一张止盈都不挂**，并**撤掉在盘 -gtp 卖单**
    （它们此刻就是"挂着的开空单"）。空仓存在时打 WARNING。
  - 覆盖口径 = **多头持仓**（不是 Σ|size| —— 旧口径把空仓当成"需要覆盖的敞口"，
    于是越挂越空）
  - 候选 = pending/filled 档（有 `tp_target`）且 `target > mark`
  - 张数上限 = `min(该档张数, 多头持仓 − 在盘 TP 已覆盖)`（绝不超配）
  - 挂单 = `POST /orders` **普通限价卖单 + `reduce_only=True`**（交易所保证只减多、
    永不反手；持仓消失时交易所会自动撤该单）
  - `target <= mark` 跳过（低于市价的限价卖单会立即成交）
  - 程序**绝不自己造价格**（`tp_target` 缺失 → 跳过留痕，交回 AI）
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from omnialpha.executor import Executor
from omnialpha.gate_client import ContractMeta


def _meta() -> ContractMeta:
    return ContractMeta(name="ETH_USDT", quanto_multiplier=0.01,
                        order_size_round=1, order_price_round=0.1, leverage_max=100)


class _Client:
    """桩：list_orders 同时承载入场买单与 -gtp 止盈卖单；get_order 按 states。"""

    def __init__(self, mark=2300.0, open_orders=None, positions=None,
                 order_states=None):
        self._mark = mark
        self._orders = list(open_orders or [])
        self._positions = list(positions if positions is not None else [])
        self._states = dict(order_states or {})
        self.placed = []
        self.cancelled = []

    def list_orders(self, contract=None):
        return list(self._orders)

    def list_price_orders(self, contract=None):
        return []

    def get_positions(self):
        return list(self._positions)

    def cancel_order(self, oid):
        self.cancelled.append(str(oid))
        return {"id": oid, "status": "cancelled"}

    def get_order(self, oid):
        st = self._states.get(str(oid))
        if st:
            return {"id": oid, **st}
        return {"id": oid, "finish_as": "", "status": "open"}

    def place_order(self, body):
        self.placed.append(body)
        return {"id": f"gtp-{len(self.placed)}", "status": "open",
                "price": body.get("price"), "size": body.get("size")}

    def get_ticker(self, sym):
        return {"mark_price": self._mark, "last": self._mark}

    def get_contract(self, sym):
        return _meta()


def _buy(oid, size=1, price=2300.0):
    """在盘入场买单（普通单，text 无 -gtp 后缀）。"""
    return {"id": str(oid), "text": "t-mtg", "size": size, "left": size,
            "status": "open", "price": str(price)}


def _gtp(oid, price, size=1):
    """在盘止盈卖单（-gtp 后缀）。"""
    return {"id": str(oid), "text": "t-mtg-gtp", "size": -abs(size),
            "status": "open", "price": str(price)}


def _pos(long=0, short=0, contract="ETH_USDT"):
    """single 模式持仓：size>0 = 多、size<0 = 空。"""
    if long:
        return [{"contract": contract, "mode": "single", "size": long,
                 "entry_price": "2300", "mark_price": "2300"}]
    if short:
        return [{"contract": contract, "mode": "single", "size": -short,
                 "entry_price": "2300", "mark_price": "2300"}]
    return []


def _lv(price, size, target, oid="o1", status="pending", tp_id=None):
    lv = {"symbol": "ETH_USDT", "price": price, "size": size, "tp_target": target,
          "label": "mtg", "order_id": oid, "ts": "2026-10-16T00:00:00Z",
          "status": status}
    if tp_id:
        lv["tp_order_id"] = tp_id
    return lv


def _new(tmp: Path, client, mode="live", **kw) -> Executor:
    return Executor(client, label_prefix="mtg", root=tmp, bot_id="mg",
                    account="", account_risk={"grid_tp_auto": mode,
                                              "auto_protect": False}, **kw)


def _levels_path(tmp: Path) -> Path:
    return tmp / "data" / "bots" / "mg" / "state" / "ladder_levels.json"


def _write_levels(tmp: Path, levels: list) -> Path:
    p = _levels_path(tmp)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"levels": levels}, ensure_ascii=False), encoding="utf-8")
    return p


def _read_levels(tmp: Path) -> list:
    return json.loads(_levels_path(tmp).read_text(encoding="utf-8"))["levels"]


class TestEnsureGridTp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    # ── 开关 ────────────────────────────────────────────────────────

    def test_off_no_action(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(), mode="off")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out["grid_tp"], "off")
        self.assertEqual(ex.client.placed, [])

    def test_dry_reports_candidates_no_order(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="dry")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out["grid_tp"], "dry")
        self.assertEqual(ex.client.placed, [])
        self.assertEqual(out["candidates"][0]["tp"], 2310.0)

    def test_manage_positions_off_skips(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(), mode="live", manage_positions=False)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out.get("skipped"), "manage_positions_off")

    # ── ⛔ 事故回归：无多头持仓 → 一张都不挂 ─────────────────────────

    def test_no_long_position_places_nothing(self):
        """**核心安全语义**：无多头持仓 → 不挂任何止盈卖单（预挂=开空）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=[]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertTrue(any("no_long_position" in str(s) for s in out["skipped"]))
        self.assertEqual(out["position_long"], 0)

    def test_short_position_blocks_and_cancels_resting_gtp(self):
        """**事故回归**：持有空仓（−14）→ 不挂，且**撤掉在盘 -gtp 卖单**。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0,
                                    open_orders=[_buy("o1"), _gtp("g-old", 2310)],
                                    positions=_pos(short=14)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertEqual(ex.client.cancelled, ["g-old"], "在盘 -gtp 必须撤掉")
        self.assertEqual(out["position_short"], 14)
        self.assertEqual(out["skipped"][0]["cancelled_gtp"], 1)

    def test_dry_does_not_cancel(self):
        """dry 模式只报告、不撤单、不下单。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0,
                                    open_orders=[_gtp("g-old", 2310)],
                                    positions=_pos(short=3)), mode="dry")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.cancelled, [])
        self.assertEqual(ex.client.placed, [])

    # ── 挂单形态：普通限价卖单 + reduce_only ────────────────────────

    def test_live_places_reduce_only_limit_sell(self):
        """**形态断言**：POST /orders 普通限价卖单 + `reduce_only=True`。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(ex.client.placed), 1)
        b = ex.client.placed[0]
        self.assertEqual(b["size"], -1)                    # 卖出方向
        self.assertEqual(float(b["price"]), 2310.0)        # 限价 = AI 目标价
        self.assertEqual(b["tif"], "gtc")
        self.assertTrue(b["text"].endswith("-gtp"))
        self.assertIs(b["reduce_only"], True)              # **必须 reduce_only**
        self.assertNotIn("trigger", b)                     # 不是条件单
        self.assertEqual(out["placed"][0]["order_type"], "limit")
        self.assertTrue(out["placed"][0]["reduce_only"])

    def test_unfilled_level_gets_tp_when_long_exists(self):
        """有多头（哪怕该档买单还在盘）→ 可挂（挂在多头之后，不是无仓预挂）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(ex.client.placed), 1)
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    def test_size_capped_by_long_position(self):
        """张数上限 = 多头持仓：档位 5 张、持仓 2 张 → 只挂 2 张。"""
        _write_levels(self.tmp, [_lv(2300, 5, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1", size=5)],
                                    positions=_pos(long=2)), mode="live")
        ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed[0]["size"], -2)

    def test_long_fully_covered_skips(self):
        """多头已被在盘 TP 完全覆盖 → 不再挂（remaining <= 0）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0,
                                    open_orders=[_buy("o1"), _gtp("g0", 2320)],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertTrue(any("long_fully_covered" in str(s) for s in out["skipped"]))

    # ── 护栏与去重 ──────────────────────────────────────────────────

    def test_below_mark_skipped(self):
        """target <= mark → 跳过（低于市价的限价卖单会立即成交）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2290)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertTrue(any("below_mark" in str(s) for s in out["skipped"]))

    def test_no_target_is_not_invented(self):
        """AI 没给目标价 → 跳过，程序绝不自己造价格。"""
        _write_levels(self.tmp, [_lv(2300, 1, None)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertTrue(any("no_tp_target" in str(s) for s in out["skipped"]))

    def test_duplicate_price_skipped(self):
        """同价止盈卖单已在盘 → 不重复挂。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0,
                                    open_orders=[_buy("o1"), _gtp("g0", 2310)],
                                    positions=_pos(long=2)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertTrue(any("tp_already_present" in str(s) for s in out["skipped"]))

    def test_mark_unavailable_skips(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=0.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertEqual(out.get("skipped"), "mark_unavailable")

    def test_order_rejected_recorded(self):
        """交易所拒单 → errors 留痕、档位不标 protected。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="live")

        def boom(body):
            raise RuntimeError("INSUFFICIENT_AVAILABLE")
        ex.client.place_order = boom
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out["placed"], [])
        self.assertTrue(out["errors"])
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "pending")

    def test_marks_level_protected_and_records_tp_id(self):
        p = _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["levels"][0]["status"], "protected")
        self.assertEqual(data["levels"][0]["tp_order_id"], "gtp-1")
        self.assertEqual(out["placed"][0]["id"], "gtp-1")

    # ── ⓪ 台账 ↔ 交易所对齐 ────────────────────────────────────────

    def test_filled_order_promoted(self):
        """入场单已成交 → 档位 promoted 为 filled。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[],
                                    positions=_pos(long=1),
                                    order_states={"o1": {"finish_as": "filled",
                                                         "status": "finished"}}),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out.get("ladder_promoted"), 1)

    def test_cancelled_order_pruned(self):
        """入场单已撤/不存在 → 档位删行（表不再只增不清）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[],
                                    positions=_pos(long=1),
                                    order_states={"o1": {"finish_as": "cancelled",
                                                         "status": "finished"}}),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out.get("ladder_pruned"), 1)
        self.assertEqual(_read_levels(self.tmp), [])

    def test_open_order_kept_pending(self):
        """买单在盘 → 不被误删。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertIsNone(out.get("ladder_pruned"))

    # ── ① protected 档的卖单簿记 ────────────────────────────────────

    def test_gtp_still_open_keeps_protected(self):
        """止盈卖单在盘 → 档位保持 protected、不重挂。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected",
                                     tp_id="gtp-x")])
        ex = _new(self.tmp, _Client(mark=2300.0,
                                    open_orders=[_buy("o1"), _gtp("gtp-x", 2310)],
                                    positions=_pos(long=1)), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    def test_gtp_filled_closes_level(self):
        """止盈卖单已成交（止盈落袋）→ 档位闭环 closed。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected",
                                     tp_id="gtp-x")])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1),
                                    order_states={"gtp-x": {"finish_as": "filled",
                                                            "status": "finished"}}),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertTrue(any(f.get("to") == "closed" for f in out["state_fixes"]))
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "closed")

    def test_gtp_cancelled_reverts_and_replaces(self):
        """**核心回归**：止盈卖单被撤（重铺/对账）→ 退回 pending 并重挂（有多头时）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected",
                                     tp_id="gtp-x")])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=_pos(long=1),
                                    order_states={"gtp-x": {"finish_as": "cancelled",
                                                            "status": "finished"}}),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(ex.client.placed), 1, "卖单被撤后必须重挂")
        self.assertEqual(float(ex.client.placed[0]["price"]), 2310.0)
        self.assertTrue(any(f.get("to") == "pending" for f in out["state_fixes"]))


if __name__ == "__main__":
    unittest.main()
