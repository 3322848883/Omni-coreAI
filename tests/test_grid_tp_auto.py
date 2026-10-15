# -*- coding: utf-8 -*-
"""阶梯档「条件限价止盈」执行器：`Executor.ensure_grid_tp` + `watcher._grid_tp_sweep`。

2026-10-15：入场腿挂单时只能带占位 TP（多单 TP 触发价必须 > mark，档位目标在现价
下方 → 非法）；档位成交后目标价才合法 → 由本机制在成交后补挂「条件限价」单。
**边界**：程序只执行 AI 已写下的 `tp_target`，绝不自己造价格。
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
    def __init__(self, finish_as="filled", mark=2300.0, price_orders=None):
        self._finish = finish_as
        self._mark = mark
        self._po = list(price_orders or [])

    def list_price_orders(self, contract=None):
        return list(self._po)

    def get_order(self, oid):
        return {"id": oid, "finish_as": self._finish}

    def get_ticker(self, sym):
        return {"mark_price": self._mark, "last": self._mark}

    def get_contract(self, sym):
        return _meta()


def _new(tmp: Path, client, mode="live", **kw) -> Executor:
    return Executor(client, label_prefix="mtg", root=tmp, bot_id="mg",
                    account="", account_risk={"grid_tp_auto": mode,
                                              "auto_protect": False}, **kw)


def _write_levels(tmp: Path, levels: list[dict]) -> Path:
    p = tmp / "data" / "bots" / "mg" / "state" / "ladder_levels.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"levels": levels}, ensure_ascii=False), encoding="utf-8")
    return p


def _lv(price, size, target, oid="o1", status="pending"):
    return {"symbol": "ETH_USDT", "price": price, "size": size, "tp_target": target,
            "label": "mtg", "order_id": oid, "ts": "2026-10-15T00:00:00Z", "status": status}


class TestEnsureGridTp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def _arm(self, ex):
        """桩掉预检与挂单，记录 _place_trigger 的入参。"""
        calls = []
        ex._precheck_exit_triggers = lambda intent: ""
        ex._place_exit_leg = lambda placer, kind, price_order: (placer(), None)
        ex._place_trigger = (lambda intent, tside, price, is_tp, meta, size: (
            calls.append({"tp_type": intent.tp_type,
                          "tp_limit_price": intent.tp_limit_price,
                          "trigger": price, "is_tp": is_tp, "size": size}),
            {"order": {"id": "tp-1", "price": price, "size": size}})[1])
        return calls

    def test_off_by_default_no_action(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(), mode="off")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out["grid_tp"], "off")
        self.assertEqual(calls, [])

    def test_dry_reports_candidates_no_order(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(mark=2300.0), mode="dry")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out["grid_tp"], "dry")
        self.assertEqual(calls, [])
        self.assertEqual(out["candidates"][0]["tp"], 2310.0)

    def test_live_places_conditional_limit_tp_at_ai_target(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(finish_as="filled", mark=2300.0), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["tp_type"], "limit")          # 条件限价（非市价）
        self.assertEqual(c["tp_limit_price"], 2310.0)     # 限价 = AI 目标价
        self.assertEqual(c["trigger"], 2310.0)            # 触发价 = AI 目标价
        self.assertTrue(c["is_tp"])
        self.assertEqual(c["size"], 1)
        self.assertEqual(out["placed"][0]["order_type"], "limit")

    def test_no_target_is_not_invented(self):
        """AI 没给目标价 → 跳过，程序绝不自己造价格。"""
        _write_levels(self.tmp, [_lv(2300, 1, None)])
        ex = _new(self.tmp, _Client(), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertTrue(any("no_tp_target" in str(s) for s in out["skipped"]))

    def test_unfilled_level_skipped(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(finish_as="cancelled"), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertEqual(out["placed"], [])

    def test_illegal_trigger_price_not_placed(self):
        """目标价在 mark 下方（多单非法）→ 报错不挂，交回 AI。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2290)])   # 2290 < mark 2300
        ex = _new(self.tmp, _Client(mark=2300.0), mode="live")
        calls = []
        ex._place_exit_leg = lambda placer, kind, price_order: (placer(), None)
        ex._place_trigger = lambda *a, **k: (calls.append(1), {"order": {"id": "x"}})[1]
        ex._precheck_exit_triggers = lambda intent: "tp=2290 需 > mark 2300（rule=1 涨破触发）"
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertTrue(out["errors"])

    def test_duplicate_target_skipped(self):
        existing = {"trigger": {"price": "2310"}, "initial": {"text": "t-mtg-tp"}}
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(price_orders=[existing]), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertTrue(any("tp_already_present" in str(s) for s in out["skipped"]))

    def test_marks_level_protected_after_place(self):
        p = _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(), mode="live")
        self._arm(ex)
        ex.ensure_grid_tp("ETH_USDT")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["levels"][0]["status"], "protected")

    def test_manage_positions_off_skips(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(), mode="live", manage_positions=False)
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertEqual(out.get("skipped"), "manage_positions_off")


class TestSweepWiring(unittest.TestCase):
    def test_sweep_exists_and_gated_off(self):
        from omnialpha import watcher
        self.assertTrue(hasattr(watcher, "_grid_tp_sweep"))

    def test_sweep_noop_when_off(self):
        from omnialpha import watcher
        class _B:
            account_risk = {"grid_tp_auto": "off"}
            bot_id = "mg"
        # mode=off 时应在构造 client 之前早退（client=None 不报错）
        watcher._grid_tp_sweep(_B(), None, client=None, symbols=[])  # 不抛即通过


class TestSkipPlaceholderTp(unittest.TestCase):
    """网格腿「去占位 TP」（2026-10-15）：只在 grid_tp_auto=live **且是网格腿**时跳过。

    官方做法（Gate/Zoomex/Hummingbot）：入场腿是纯限价买单、不挂 TP；成交后由
    `ensure_grid_tp` 按 `meta.tp_target` 挂反向单 —— 占位 TP 是假网格的根源。
    """

    def test_live_and_grid_leg_skips(self):
        from omnialpha.executor import _skip_placeholder_tp
        self.assertTrue(_skip_placeholder_tp({"grid_tp_auto": "live"}, {"tp_target": 2367.0}))
        self.assertTrue(_skip_placeholder_tp({"grid_tp_auto": True}, {"tp_target": 1.0}))

    def test_live_but_martin_leg_keeps_placeholder(self):
        """马丁腿（无 tp_target）照挂共用 TP —— 设计需求，不动。"""
        from omnialpha.executor import _skip_placeholder_tp
        self.assertFalse(_skip_placeholder_tp({"grid_tp_auto": "live"}, {"tp_target": None}))
        self.assertFalse(_skip_placeholder_tp({"grid_tp_auto": "live"}, {}))

    def test_dry_and_off_keep_placeholder(self):
        """dry/off 下不能跳过 —— 否则档位彻底没有止盈。"""
        from omnialpha.executor import _skip_placeholder_tp
        for m in ("dry", "off", False, None):
            with self.subTest(mode=m):
                self.assertFalse(_skip_placeholder_tp({"grid_tp_auto": m}, {"tp_target": 2367.0}))


if __name__ == "__main__":
    unittest.main()
