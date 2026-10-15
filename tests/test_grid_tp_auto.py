# -*- coding: utf-8 -*-
"""阶梯档「条件限价止盈」执行器：`Executor.ensure_grid_tp` + `watcher._grid_tp_sweep`。

2026-10-15：入场腿挂单时只能带占位 TP（多单 TP 触发价必须 > mark，档位目标在现价
下方 → 非法）；档位成交后目标价才合法 → 由本机制在成交后补挂「条件限价」单。
**边界**：程序只执行 AI 已写下的 `tp_target`，绝不自己造价格。

2026-10-16 补强（A2，实测事故驱动）：
  ① **台账 ↔ 交易所对齐**：pending 档的入场单不在盘 → 已成交标 `filled`、已撤删行
     （原先只增不清，实测 5 小时涨到 110 条）；
  ② **protected 档的 TP 校验**：`get_price_order` 查 TP 单终态 —— `succeeded`（成交）
     / `failed`（无仓可减）→ 该档闭环 `closed`；`cancelled`（被撤）→ 退回 `pending`
     重挂（原先只扫 pending 档，TP 被撤后该档**永久失去止盈**）；
  ③ **覆盖校验**：在盘 TP 合计 < 持仓 → 从待挂档补（最新成交优先）；无持仓则不动。
"""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from omnialpha.executor import Executor
from omnialpha.gate_client import ContractMeta


def _meta() -> ContractMeta:
    return ContractMeta(name="ETH_USDT", quanto_multiplier=0.01,
                        order_size_round=1, order_price_round=0.1, leverage_max=100)


class _Client:
    """桩客户端。默认：1 张多仓、入场单已成交、条件单终态 = cancelled。"""

    def __init__(self, finish_as="filled", mark=2300.0, price_orders=None,
                 positions=None, po_status=None, open_orders=None):
        self._finish = finish_as
        self._mark = mark
        self._po = list(price_orders or [])
        self._positions = ([{"contract": "ETH_USDT", "size": 1}]
                           if positions is None else list(positions))
        self._po_status = dict(po_status or {})
        self._orders = list(open_orders or [])

    def list_price_orders(self, contract=None):
        return list(self._po)

    def list_orders(self, contract=None):
        return list(self._orders)

    def get_positions(self):
        return list(self._positions)

    def get_order(self, oid):
        return {"id": oid, "finish_as": self._finish}

    def get_price_order(self, oid):
        return {"id": oid, "finish_as": self._po_status.get(str(oid), "cancelled")}

    def get_ticker(self, sym):
        return {"mark_price": self._mark, "last": self._mark}

    def get_contract(self, sym):
        return _meta()


def _new(tmp: Path, client, mode="live", **kw) -> Executor:
    return Executor(client, label_prefix="mtg", root=tmp, bot_id="mg",
                    account="", account_risk={"grid_tp_auto": mode,
                                              "auto_protect": False}, **kw)


def _levels_path(tmp: Path) -> Path:
    return tmp / "data" / "bots" / "mg" / "state" / "ladder_levels.json"


def _write_levels(tmp: Path, levels: list[dict]) -> Path:
    p = _levels_path(tmp)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"levels": levels}, ensure_ascii=False), encoding="utf-8")
    return p


def _read_levels(tmp: Path) -> list[dict]:
    return json.loads(_levels_path(tmp).read_text(encoding="utf-8"))["levels"]


def _lv(price, size, target, oid="o1", status="pending", tp_id=None,
        ts="2026-10-15T00:00:00Z"):
    lv = {"symbol": "ETH_USDT", "price": price, "size": size, "tp_target": target,
          "label": "mtg", "order_id": oid, "ts": ts, "status": status}
    if tp_id:
        lv["tp_order_id"] = tp_id
    return lv


def _open_tp(trig, size, oid="tp-open"):
    return {"id": oid, "trigger": {"price": str(trig)},
            "initial": {"text": "t-mtg-tp", "size": -abs(size), "is_reduce_only": True}}


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

    # ── 基本行为 ────────────────────────────────────────────────────

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
        """同价已有 TP → 不重复挂（覆盖已足 → 直接跳过；缺覆盖 → 按触发价去重）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(price_orders=[_open_tp(2310, 1)]), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertTrue(any(s.get("why") == "covered_enough" for s in out["skipped"]))

        # 持仓 2 张、在盘 TP 只覆盖 1 张 → 进入候选评估，但同价已在盘 → 去重跳过
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex2 = _new(self.tmp, _Client(positions=[{"contract": "ETH_USDT", "size": 2}],
                                     price_orders=[_open_tp(2310, 1)]), mode="live")
        calls2 = self._arm(ex2)
        out2 = ex2.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls2, [])
        self.assertTrue(any("tp_already_present" in str(s) for s in out2["skipped"]))

    def test_marks_level_protected_and_records_tp_id(self):
        p = _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(), mode="live")
        self._arm(ex)
        ex.ensure_grid_tp("ETH_USDT")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["levels"][0]["status"], "protected")
        self.assertEqual(data["levels"][0]["tp_order_id"], "tp-1")

    def test_manage_positions_off_skips(self):
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        ex = _new(self.tmp, _Client(), mode="live", manage_positions=False)
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertEqual(out.get("skipped"), "manage_positions_off")

    # ── A2-②：protected 档的 TP 校验 ─────────────────────────────────

    def test_cancelled_tp_reverts_to_pending_and_replaces(self):
        """**核心回归**：逐档 TP 被撤（对账/重铺）→ 退回待挂并重挂。

        实测 2026-10-15：档位 2423.7 的 TP（id 2110771294122283008）挂出 4 分钟被
        旧对账撤掉，档位表仍标 protected → 从此不再补挂。本用例钉住修复。
        """
        _write_levels(self.tmp, [_lv(2423.7, 1, 2438.0, status="protected", tp_id="tp-old")])
        ex = _new(self.tmp, _Client(po_status={"tp-old": "cancelled"}), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(calls), 1, "TP 被撤后必须重挂")
        self.assertEqual(calls[0]["trigger"], 2438.0)
        self.assertTrue(any(f.get("to") == "pending" for f in out["state_fixes"]))
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")
        self.assertEqual(_read_levels(self.tmp)[0]["tp_order_id"], "tp-1")

    def test_succeeded_tp_closes_level(self):
        """TP 已成交（止盈了结）→ 档位闭环 closed，不再重挂。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected", tp_id="tp-old")])
        ex = _new(self.tmp, _Client(positions=[], po_status={"tp-old": "succeeded"}),
                  mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertTrue(any(f.get("to") == "closed" for f in out["state_fixes"]))
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "closed")

    def test_failed_tp_retried_while_position_remains(self):
        """TP 被交易所判 failed（未执行）而持仓仍在 → 退回待挂并重挂（覆盖不丢）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected", tp_id="tp-old")])
        ex = _new(self.tmp, _Client(positions=[{"contract": "ETH_USDT", "size": 1}],
                                    po_status={"tp-old": "failed"}),
                  mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(calls), 1, "持仓仍在 → 必须重挂")
        self.assertTrue(any(f.get("why") == "tp_failed" for f in out["state_fixes"]))
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    def test_tp_still_open_keeps_protected(self):
        """TP 还在盘 → 保持 protected，不重复挂。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected", tp_id="tp-open")])
        ex = _new(self.tmp, _Client(price_orders=[_open_tp(2310, 1)]), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertEqual(out["state_fixes"], [])
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "protected")

    def test_legacy_protected_without_tp_id(self):
        """老行（无 tp_order_id）：无仓 → closed；有仓 → 退回 pending 重挂。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected")])
        ex = _new(self.tmp, _Client(positions=[]), mode="live")
        self._arm(ex)
        ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(_read_levels(self.tmp)[0]["status"], "closed")

        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="protected")])
        ex2 = _new(self.tmp, _Client(positions=[{"contract": "ETH_USDT", "size": 1}]),
                   mode="live")
        calls = self._arm(ex2)
        ex2.ensure_grid_tp("ETH_USDT")
        self.assertEqual(len(calls), 1)

    # ── A2-③：覆盖校验 ──────────────────────────────────────────────

    def test_covered_enough_no_replace(self):
        """在盘 TP 合计 ≥ 持仓 → 不重挂（covered_enough）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, oid="o1")])
        ex = _new(self.tmp, _Client(price_orders=[_open_tp(2310, 1)],
                                    open_orders=[{"id": "o1", "text": "t-mtg",
                                                  "is_reduce_only": False}]),
                  mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertTrue(any(s.get("why") == "covered_enough" for s in out["skipped"]))

    def test_flat_places_nothing_and_closes_levels(self):
        """无持仓 → 一张不挂，filled/protected 档闭环。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, status="filled"),
                                 _lv(2280, 1, 2290, status="protected", tp_id="tp-old")])
        ex = _new(self.tmp, _Client(positions=[]), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertTrue(any(s.get("why") == "no_position" for s in out["skipped"]))
        self.assertTrue(all(x["status"] == "closed" for x in _read_levels(self.tmp)))

    def test_position_query_failure_is_fail_closed(self):
        """持仓取不到 → 不动（fail-closed）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310)])
        client = _Client()
        client.get_positions = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        ex = _new(self.tmp, client, mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(calls, [])
        self.assertEqual(out.get("skipped"), "positions_unavailable")

    # ── A2-①：台账 ↔ 交易所对齐（只增不清）──────────────────────────

    def test_stale_pending_row_pruned(self):
        """入场单已撤（不在盘）→ 删掉该行（原先永久留 pending）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, oid="o-gone"),
                                 _lv(2280, 1, 2290, oid="o-live")])
        ex = _new(self.tmp, _Client(finish_as="cancelled",
                                    open_orders=[{"id": "o-live", "text": "t-mtg",
                                                  "is_reduce_only": False}]),
                  mode="live")
        self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        left = [x["order_id"] for x in _read_levels(self.tmp)]
        self.assertIn("o-live", left)
        self.assertNotIn("o-gone", left)
        self.assertEqual(out.get("ladder_pruned"), 1)

    def test_pending_entry_filled_promoted(self):
        """入场单已成交（不在盘）→ 标 filled（成为覆盖校验的候选）。"""
        _write_levels(self.tmp, [_lv(2300, 1, 2310, oid="o-filled")])
        ex = _new(self.tmp, _Client(finish_as="filled"), mode="live")
        calls = self._arm(ex)
        out = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out.get("ladder_promoted"), 1)
        self.assertEqual(len(calls), 1, "标 filled 后应被覆盖校验补挂")


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


class TestReconcileSweepLogging(unittest.TestCase):
    """2026-10-16：对账的静默早退补日志（原先 ok=False / skipped 完全无据可查）。"""

    def _run(self, res, tmp):
        from omnialpha import reconcile, watcher
        bot = types.SimpleNamespace(bot_id="mg", label_prefix="mtg", account="",
                                    account_risk={}, manage_positions=True,
                                    allow_close=True, allow_reduce=True,
                                    create_client=lambda: object(),
                                    symbols=["ETH_USDT"])
        paths = types.SimpleNamespace(root=tmp)
        with mock.patch.object(reconcile, "reconcile_protectors", return_value=res), \
             mock.patch.object(watcher, "log") as lg, \
             mock.patch.object(watcher, "_sweep_symbols", return_value=["ETH_USDT"]):
            watcher._reconcile_sweep(bot, paths, client=object(), symbols=["ETH_USDT"])
        return lg

    def test_failure_logged_as_warning(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._run({"ok": False, "error": "positions down"}, Path(td))
        self.assertTrue(any("取数失败" in str(c) for c in lg.warning.call_args_list),
                        "取数失败必须留日志（fail-closed 但不能静默）")

    def test_skip_logged_as_debug(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._run({"ok": True, "skipped": "no_position", "cancelled": []}, Path(td))
        self.assertTrue(any("skipped" in str(c) for c in lg.debug.call_args_list))

    def test_dedup_logged_as_info(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._run({"ok": True, "cancelled": ["a", "b"], "kept": 2,
                            "tp_legs": 1, "dedup": 2, "capped": 0}, Path(td))
        self.assertTrue(any("清理重复保护单" in str(c) for c in lg.info.call_args_list))


class TestSkipPlaceholderTp(unittest.TestCase):
    """网格腿「去占位 TP」（2026-10-15）：只在 grid_tp_auto=live **且是网格腿**时跳过。"""

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


class _ChainClient(_Client):
    """可变桩：`place_price_order` 真把单挂进在盘集合（`_po`），供全链验证。

    返回 `{\"id\": ...}`（Gate `/price_orders` 的顶层订单体形态）——
    `_place_exit_leg` 会把它归一成 `{\"order\": {...}}` 并写进台账 `tp_order_id`。
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.tp_bodies = []
        self._tp_seq = 0

    def place_price_order(self, body):
        self._tp_seq += 1
        oid = f"tp-{self._tp_seq}"
        self.tp_bodies.append(json.loads(json.dumps(body)))
        self._po.append(dict(body, id=oid))
        return {"id": oid}


class TestSingleModeTenLayers(unittest.TestCase):
    """⚠️ 单向（single）模式下「**每层网格 → 各自止盈**」的完整链路。

    `paper.position_mode` 默认就是 single（config.py:141）—— 实盘 2026-10-16
    切 single 后，这条链路与实盘**同构**。持仓形态用 single 真实结构：
    一条记录、`mode="single"`（无方向后缀）、方向靠 size 推导
    （推导本身由 tests/test_position_mode.py 锁死）。

    链路：10 层全成交 → `ensure_grid_tp` 逐层挂条件限价 TP → 去重 →
    全部触发成交 → 档位闭环。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def test_ten_layers_each_get_own_tp_then_dedup_then_close_loop(self):
        prices = [2355, 2340, 2325, 2310, 2295, 2280, 2265, 2250, 2235, 2220]
        _write_levels(self.tmp, [_lv(p, 1, p + 12, oid=f"o{i}")
                                 for i, p in enumerate(prices)])
        cli = _ChainClient(finish_as="filled", mark=2200.0,
                           positions=[{"contract": "ETH_USDT", "size": 10,
                                       "mode": "single"}])   # ⚠️ single 真实形态
        ex = _new(self.tmp, cli, mode="live")
        ex._precheck_exit_triggers = lambda intent: ""   # 预检另有专项，这里聚焦挂单链路

        out = ex.ensure_grid_tp("ETH_USDT")

        # 1) 10 层各挂一条（不是 1 条共用、不是占位价）
        self.assertEqual(len(out["placed"]), 10, out)
        # 2) 触发价 = 各层自己的目标，互不相同 —— 「每层对应止盈」的本体
        trigs = {b["trigger"]["price"] for b in cli.tp_bodies}
        self.assertEqual(len(trigs), 10)
        self.assertEqual({float(t) for t in trigs}, {p + 12 for p in prices})
        # 3) 每条：条件限价、reduce_only、gtc、限价 = 触发价、卖 1 张（平多）
        for b in cli.tp_bodies:
            ini, tri = b["initial"], b["trigger"]
            self.assertTrue(ini.get("reduce_only") or ini.get("is_reduce_only"), b)
            self.assertEqual(ini.get("tif"), "gtc")
            self.assertEqual(float(ini["price"]), float(tri["price"]))
            self.assertEqual(abs(int(ini["size"])), 1)
            self.assertLess(int(ini["size"]), 0, "平多 = 负 size")
        # 4) 合计 = 持仓（10 张，不多不少）
        self.assertEqual(sum(int(b["initial"]["size"]) for b in cli.tp_bodies), -10)
        # 5) 台账：全 protected 且带 tp_order_id（缺了 F1 的终态校验就失效）
        lv = _read_levels(self.tmp)
        self.assertTrue(all(x["status"] == "protected" for x in lv), lv)
        self.assertTrue(all(x.get("tp_order_id") for x in lv), lv)
        # 6) 再跑一轮 → 不重复挂（covered=10=pos → covered_enough）
        out2 = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out2["placed"], [])
        self.assertTrue(any(s.get("why") == "covered_enough"
                            for s in out2["skipped"]), out2["skipped"])
        # 7) 10 条 TP 全部触发成交（succeeded）+ 持仓归 0 → 全部闭环
        for x in cli._po:
            cli._po_status[x["id"]] = "succeeded"
        cli._po = []
        cli._positions = [{"contract": "ETH_USDT", "size": 0, "mode": "single"}]
        out3 = ex.ensure_grid_tp("ETH_USDT")
        self.assertEqual(out3["placed"], [])
        lv2 = _read_levels(self.tmp)
        self.assertTrue(all(x["status"] == "closed" for x in lv2), lv2)

    def test_martin_layers_without_target_left_to_ai(self):
        """马丁层无 tp_target → 跳过交回 AI（程序绝不自己造价格）；网格层照挂。"""
        prices = [2355, 2340, 2325]
        levels = [_lv(p, 1, p + 12, oid=f"o{i}") for i, p in enumerate(prices)]
        levels += [_lv(2290, 6, None, oid="m1"),      # 马丁层：无目标
                   _lv(2250, 7, None, oid="m2")]
        _write_levels(self.tmp, levels)
        cli = _ChainClient(finish_as="filled", mark=2200.0,
                           positions=[{"contract": "ETH_USDT", "size": 17,
                                       "mode": "single"}])
        ex = _new(self.tmp, cli, mode="live")
        ex._precheck_exit_triggers = lambda intent: ""

        out = ex.ensure_grid_tp("ETH_USDT")

        # 只挂 3 个网格层；马丁层跳过并留痕（交回 AI）
        self.assertEqual(len(out["placed"]), 3, out)
        self.assertEqual({c["tp"] for c in out["candidates"]},
                         {p + 12 for p in prices})
        self.assertTrue(any("no_tp_target" in str(s) for s in out["skipped"]))
        # 马丁层保持 filled（不被标 protected）
        lv = _read_levels(self.tmp)
        by_price = {x["price"]: x["status"] for x in lv}
        self.assertEqual(by_price[2290], "filled")
        self.assertEqual(by_price[2250], "filled")


if __name__ == "__main__":
    unittest.main()
