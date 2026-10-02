# -*- coding: utf-8 -*-
"""推送卡片字段完整性：用真实线上 detail 形状做回归。

背景：卡片格式化函数曾按「扁平字段」读取（detail.size_usd / detail.tp / detail.sl），
但实盘（Gate）不同 action 的 detail 形状并不一致 ——
  open_*        → detail.tp / detail.sl / detail.size_usd（有）
  modify_tp_sl  → detail.tp_placed.price / detail.sl_placed.price（没有 detail.tp）
  stop_entry_*  → 只有 tp_orders[] / sl_orders[]（连 size_usd 都没有）
  reduce_*      → 只有 order.size（张数）
结果卡片上「止盈 / 止损 / 仓位」渲染成 —，也就是用户看到的「缺失数据」。

下面每个 fixture 都是照线上 archive/done/*.result.json 的真实结构抄的。
"""
from __future__ import annotations

import unittest

from omnialpha.monitoring.notify import (
    format_trade_card,
    format_trade_steps,
    _resolve_pnl,
    _resolve_size,
    _resolve_sl,
    _resolve_tp,
)

# ── 真实形状（抄自 /opt/omnialpha/data/bots/brooks-btc/archive/done/）──────────

LIVE_MODIFY = {
    "action": "modify_tp_sl", "symbol": "BTC_USDT", "ok": True, "error": "",
    "detail": {
        "position_side": "long", "position_size": 51, "prefix": "t-brk",
        "tp_placed": {"id": "2105916765417508864", "price": 86800.0,
                      "check": {"kind": "tp", "ok": True}},
        "sl_placed": {"id": "2105916769263685632", "price": 85800.0,
                      "check": {"kind": "sl", "ok": True}},
        "cancelled_old": ["2105912662960377856", "2105912659231641600"],
    },
}

LIVE_OPEN = {
    "action": "open_long", "symbol": "BTC_USDT", "ok": True, "error": "",
    "detail": {
        "order": {"id": 36028837204877332, "price": "85650", "size": 19, "left": 19,
                  "fill_price": 0, "status": "open", "text": "t-brk"},
        "contracts": 19, "size_usd": 167.61063662877768, "order_type": "limit",
        "price": 85650.0, "entry_price": 85650.0, "sl": 85200.0, "tp": 86150.0,
        "tp2": 86800.0,
        "tp_orders": [{"trigger_price": 86150.0}, {"trigger_price": 86800.0}],
        "sl_orders": [{"trigger_price": 85200.0}],
    },
}

LIVE_STOP_ENTRY = {
    "action": "stop_entry_long", "symbol": "BTC_USDT", "ok": True, "error": "",
    "detail": {
        "order": {"id": 2105821596483588096, "price": "83840", "size": 29, "left": 29},
        "order_check": {"kind": "stop_entry", "id": "2105821596483588096", "ok": True},
        "tp_orders": [{"trigger_price": 84150.0}, {"trigger_price": 84600.0}],
        "sl_orders": [{"trigger_price": 83400.0}],
        "exit_errors": [], "hang_mode": "with_stop_entry",
    },
}

LIVE_REDUCE = {
    "action": "reduce_long", "symbol": "BTC_USDT", "ok": True, "error": "",
    "detail": {
        "order": {"size": -16, "fill_price": "83425.9", "price": "0", "status": "filled"},
        "side": "long", "position_mode": "dual", "executed_as": "close",
        "cleaned_protectors": [], "resized_protectors": [],
        "mode_note": "dual: close this side only",
    },
}

LIVE_CLOSE = {
    "action": "close_long", "symbol": "BTC_USDT", "ok": True, "error": "",
    "detail": {
        "order": {"size": -51, "fill_price": "86200.5", "avg_price": 86200.5,
                  "pnl": 12.34, "status": "filled"},
        "side": "long", "executed_as": "close",
    },
}


def _fields(card: dict) -> dict:
    """把卡片 elements 摊平成 {字段名: 值}。"""
    out = {}
    for el in card.get("elements", []):
        for f in el.get("fields", []):
            k, _, v = f["text"]["content"].partition(":**")
            out[k.strip("* ")] = v.strip()
    return out


class TestResolvers(unittest.TestCase):
    def test_tp_from_placed(self):
        d = LIVE_MODIFY["detail"]
        self.assertEqual(_resolve_tp(d, LIVE_MODIFY), 86800.0)

    def test_sl_from_placed(self):
        d = LIVE_MODIFY["detail"]
        self.assertEqual(_resolve_sl(d, LIVE_MODIFY), 85800.0)

    def test_tp_sl_from_legs_when_no_direct_field(self):
        d = LIVE_STOP_ENTRY["detail"]
        self.assertEqual(_resolve_tp(d, LIVE_STOP_ENTRY), 84150.0)
        self.assertEqual(_resolve_sl(d, LIVE_STOP_ENTRY), 83400.0)

    def test_direct_field_wins(self):
        d = LIVE_OPEN["detail"]
        self.assertEqual(_resolve_tp(d, LIVE_OPEN), 86150.0)
        self.assertEqual(_resolve_sl(d, LIVE_OPEN), 85200.0)

    def test_size_usd_preferred(self):
        self.assertEqual(_resolve_size(LIVE_OPEN["detail"], LIVE_OPEN),
                         ("167.61", "USDT"))

    def test_size_falls_back_to_contracts(self):
        self.assertEqual(_resolve_size(LIVE_REDUCE["detail"], LIVE_REDUCE), ("16", "张"))

    def test_size_missing_is_dash(self):
        self.assertEqual(_resolve_size({}, {}), ("—", ""))

    def test_pnl_accepts_zero(self):
        self.assertEqual(_resolve_pnl({"realized_pnl": 0}, {}), 0)

    def test_pnl_from_order(self):
        self.assertEqual(_resolve_pnl(LIVE_CLOSE["detail"], LIVE_CLOSE), 12.34)


class TestCardNoMissingFields(unittest.TestCase):
    """真实形状下，卡片不该出现任何「—」（这就是用户报的缺失数据）。"""

    def _assert_no_dash(self, step: dict):
        cards = format_trade_card("brooks-btc", [step])
        self.assertEqual(len(cards), 1, f"{step['action']} 应产出一张卡片")
        f = _fields(cards[0])
        empty = {k: v for k, v in f.items() if v in ("—", "", "USDT", "张")}
        self.assertEqual(empty, {}, f"{step['action']} 卡片有缺失字段: {empty} (全部={f})")
        return f

    def test_modify_tp_sl_shows_prices(self):
        f = self._assert_no_dash(LIVE_MODIFY)
        self.assertEqual(f["止盈"], "86800")
        self.assertEqual(f["止损"], "85800")

    def test_open_long_shows_all(self):
        f = self._assert_no_dash(LIVE_OPEN)
        self.assertEqual(f["入场价"], "85650")
        self.assertEqual(f["仓位"], "167.61 USDT")
        self.assertEqual(f["止损"], "85200")
        self.assertEqual(f["止盈"], "86150")

    def test_stop_entry_shows_all(self):
        f = self._assert_no_dash(LIVE_STOP_ENTRY)
        self.assertEqual(f["止盈"], "84150")
        self.assertEqual(f["止损"], "83400")

    def test_reduce_shows_contracts(self):
        f = self._assert_no_dash(LIVE_REDUCE)
        self.assertEqual(f["减仓价"], "83425.9")
        self.assertEqual(f["减仓量"], "16 张")

    def test_close_shows_pnl(self):
        f = self._assert_no_dash(LIVE_CLOSE)
        self.assertEqual(f["平仓价"], "86200.5")
        self.assertEqual(f["盈亏"], "12.34 USDT")

    def test_steps_text_no_missing(self):
        for step in (LIVE_MODIFY, LIVE_OPEN, LIVE_STOP_ENTRY, LIVE_REDUCE):
            with self.subTest(action=step["action"]):
                lines = format_trade_steps("brooks-btc", [step])
                self.assertEqual(len(lines), 1)
                self.assertNotIn("—", lines[0], lines[0])
                self.assertNotIn("= ", lines[0], lines[0])
                self.assertNotIn("size= ", lines[0], lines[0])


if __name__ == "__main__":
    unittest.main()
