# -*- coding: utf-8 -*-
"""阶梯档止盈执行器：单向模式「普通限价卖单」形态（2026-10-16 用户拍板）。

**语义变更（实测驱动）**：止盈不再是条件触发单（price_orders）——
无持仓实测三种形态：条件单被 `AUTO_TRIGGER_PRICE_GREATE_MARK` 拒（触发价必须>mark）、
reduce_only 普通单被 `INCREASE_POSITION` 拒（无仓可减）、**普通限价卖单成功挂上**
（id=63894859046511652，挂上即撤）→ 只有普通卖单能把整个网格的止盈「完全挂完」。

**新语义**：
  - 候选 = pending/filled 网格档（有 `tp_target`）且 **target > mark** ——
    **不再要求该档已成交**（先挂上 = 用户要求的「完全挂完」）
  - 挂单 = `POST /orders` **普通限价卖单**（text 后缀 `-gtp`、无 reduce_only）
  - `target <= mark` 跳过（低于市价的限价卖单会立即成交 = 直接开空）—— 唯一护栏
  - ⚠️ 语义（用户明确接受）：卖单是方向性订单 —— 有持仓时成交 = 平多止盈；
    无持仓时成交 = 开空
  - 无持仓**不再**整体闭环（预挂形态不依赖持仓）

保留不变：⓪ 台账 ↔ 交易所对齐（filled 提升 / cancelled 删行 / 在盘保留）。
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

    def list_orders(self, contract=None):
        return list(self._orders)

    def list_price_orders(self, contract=None):
        return []

    def get_positions(self):
        return list(self._positions)

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
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")]), mode="dry")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out["grid_tp"], "dry")
        self.assertEqual(ex.client.placed, [])
        self.assertEqual(out["candidates"][0]["tp"], 2310.0)

    def test_manage_positions_off_skips(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(), mode="live", manage_positions=False)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out.get("skipped"), "manage_positions_off")

    # ── 挂单形态：普通限价卖单 ──────────────────────────────────────

    def test_live_places_plain_limit_sell(self):
        """**形态断言**：POST /orders 普通限价卖单 —— 无 reduce_only、text=-gtp。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(ex.client.placed), 1)
        b = ex.client.placed[0]
        self.assertEqual(b["size"], -1)                    # 卖出方向
        self.assertEqual(float(b["price"]), 2310.0)        # 限价 = AI 目标价
        self.assertEqual(b["tif"], "gtc")
        self.assertTrue(b["text"].endswith("-gtp"))
        self.assertNotIn("reduce_only", b)                 # 无 reduce_only 标记
        self.assertNotIn("trigger", b)                     # 不是条件单
        self.assertEqual(out["placed"][0]["order_type"], "limit")
        self.assertFalse(out["placed"][0]["reduce_only"])

    def test_unfilled_level_still_gets_tp(self):
        """**核心新语义**：档位未成交（买单在盘等跌）→ 止盈也先挂上（完全挂完）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])       # 买单在盘、未成交
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(ex.client.placed), 1)
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    def test_no_position_still_places(self):
        """无持仓也挂（预挂形态）—— 旧逻辑的 no_position 早退已删除。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    positions=[]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(ex.client.placed), 1)

    def test_all_grid_levels_placed_at_once(self):
        """7 个网格档目标全在 mark 上方 → 一次全挂（用户的「完全挂完」）。"""
        levels, buys = [], []
        for i, (px, tg) in enumerate([(2300, 2310), (2285, 2300), (2270, 2285),
                                      (2255, 2270), (2240, 2255), (2225, 2240),
                                      (2210, 2225)]):
            levels.append(_lv(px, 1, tg, oid=f"o{i}"))
            buys.append(_buy(f"o{i}", price=px))
        _write_levels(self.tmp, levels)
        ex = _new(self.tmp, _Client(mark=2200.0, open_orders=buys), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(ex.client.placed), 7)
        self.assertEqual(len(out["placed"]), 7)
        prices = sorted(float(b["price"]) for b in ex.client.placed)
        self.assertEqual(prices, [2225.0, 2240.0, 2255.0, 2270.0, 2285.0, 2300.0, 2310.0])

    # ── 护栏与去重 ──────────────────────────────────────────────────

    def test_below_mark_skipped_never_opens_short(self):
        """target <= mark → 跳过：低于市价的限价卖单会立即成交 = 直接开空。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2290)])   # 2290 < mark 2300
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertTrue(any("below_mark" in str(s) for s in out["skipped"]))

    def test_no_target_is_not_invented(self):
        """AI 没给目标价 → 跳过，程序绝不自己造价格。"""
        _write_levels(self.tmp, [_lv(2300, 1, None)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertTrue(any("no_tp_target" in str(s) for s in out["skipped"]))

    def test_duplicate_price_skipped(self):
        """同价止盈卖单已在盘 → 不重复挂。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0,
                                    open_orders=[_buy("o1"), _gtp("g0", 2310)]),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertTrue(any("tp_already_present" in str(s) for s in out["skipped"]))

    def test_mark_unavailable_skips(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=0.0, open_orders=[_buy("o1")]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertEqual(out.get("skipped"), "mark_unavailable")

    def test_order_rejected_recorded(self):
        """交易所拒单 → errors 留痕、档位不标 protected。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")]), mode="live")

        def boom(body):
            raise RuntimeError("INSUFFICIENT_AVAILABLE")
        ex.client.place_order = boom
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out["placed"], [])
        self.assertTrue(out["errors"])
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "pending")

    def test_marks_level_protected_and_records_tp_id(self):
        p = _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["levels"][0]["status"], "protected")
        self.assertEqual(data["levels"][0]["tp_order_id"], "gtp-1")
        self.assertEqual(out["placed"][0]["id"], "gtp-1")

    # ── ⓪ 台账 ↔ 交易所对齐（行为不变）────────────────────────────

    def test_filled_order_promoted(self):
        """入场单已成交 → 档位 promoted 为 filled（仍参与候选）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[],       # 不在盘
                                    order_states={"o1": {"finish_as": "filled",
                                                         "status": "finished"}}),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out.get("ladder_promoted"), 1)
        self.assertEqual(len(ex.client.placed), 1, "filled 档也参与候选")
        # promoted 后立即挂上止盈 → protected（新语义：成交档优先获得止盈）
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    def test_cancelled_order_pruned(self):
        """入场单已撤/不存在 → 档位删行（表不再只增不清）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[],
                                    order_states={"o1": {"finish_as": "cancelled",
                                                         "status": "finished"}}),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out.get("ladder_pruned"), 1)
        self.assertEqual(_read_levels(self.tmp), [])

    def test_open_order_kept_pending(self):
        """买单在盘 → 不被误删，且立即挂上止盈（pending → protected）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertIsNone(out.get("ladder_pruned"))
        self.assertEqual(len(ex.client.placed), 1)
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    # ── ① protected 档的卖单簿记 ────────────────────────────────────

    def test_gtp_still_open_keeps_protected(self):
        """止盈卖单在盘 → 档位保持 protected、不重挂。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected",
                                     tp_id="gtp-x")])
        ex = _new(self.tmp, _Client(mark=2300.0,
                                    open_orders=[_buy("o1"), _gtp("gtp-x", 2310)]),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    def test_gtp_filled_closes_level(self):
        """止盈卖单已成交（止盈落袋）→ 档位闭环 closed。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected",
                                     tp_id="gtp-x")])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    order_states={"gtp-x": {"finish_as": "filled",
                                                            "status": "finished"}}),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertTrue(any(f.get("to") == "closed" for f in out["state_fixes"]))
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "closed")

    def test_gtp_cancelled_reverts_and_replaces(self):
        """**核心回归**：止盈卖单被撤（重铺/对账）→ 退回 pending 并重挂。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected",
                                     tp_id="gtp-x")])
        ex = _new(self.tmp, _Client(mark=2300.0, open_orders=[_buy("o1")],
                                    order_states={"gtp-x": {"finish_as": "cancelled",
                                                            "status": "finished"}}),
                  mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(ex.client.placed), 1, "卖单被撤后必须重挂")
        self.assertEqual(float(ex.client.placed[0]["price"]), 2310.0)
        self.assertTrue(any(f.get("to") == "pending" for f in out["state_fixes"]))
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    def test_no_position_no_longer_closes_levels(self):
        """无持仓时 protected 档**不再被强制闭环**（预挂卖单不依赖持仓）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected",
                                     tp_id="gtp-x")])
        ex = _new(self.tmp, _Client(mark=2300.0,
                                    open_orders=[_gtp("gtp-x", 2310)],
                                    positions=[]), mode="live")
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(ex.client.placed, [])
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected",
                         "无持仓不得闭环 —— 卖单是预挂形态")


if __name__ == "__main__":
    unittest.main()
