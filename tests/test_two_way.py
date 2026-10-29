# -*- coding: utf-8 -*-
"""双向阶梯：多头铺下方 / 空头铺上方 / auto 动态判方向 / 方向切换。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

MARK = 2332.71
ATR = 7.5


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark=MARK, pos=("long", 3.0)):
        self.mark = mark
        self.pos = pos
        self.book, self.prot, self.cancelled = [], [], []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        side, size = self.pos
        if not size:
            return []
        # size 必须带符号（真实 Gate `/positions.size` 空头为负；恒正会让
        # `position_qty` 把空头读成多头 → 反向持仓护栏失效）
        signed = abs(size) if side == "long" else -abs(size)
        return [{"contract": "ETH_USDT", "side": side, "size": str(signed),
                 "entry_price": str(self.mark)}]

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-mtg"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        self.book.append(dict(body, id=f"o{len(self.book)}"))
        return {"id": f"o{len(self.book)}"}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        self.prot = [p for p in self.prot if p["id"] != pid]
        return {}


class Report:
    def __init__(self):
        self.results = []


def db(mark=MARK, bb=None, ema20=None, ema50=None):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema20 REAL, ema50 REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, mark, mark + 5, mark - 5, mark,
                 ATR, bb if bb is not None else mark,
                 mark * 1.013, mark * 0.987, ema20, ema50))
    con.commit()
    con.close()
    return f.name


TPL = {
    "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
    "martin_slots": {3: 6, 7: 7, 10: 9}, "snap_tol_pct": 0.15, "martin": [],
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
}


class TestTwoWay(unittest.TestCase):
    def _run(self, action, mark=MARK, pos=("long", 3.0), extra=None, dbkw=None):
        c = Client(mark=mark, pos=pos)
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        tpl = dict(TPL, dynamic=dict(TPL["dynamic"], db_path=db(mark, **(dbkw or {}))))
        tpl.update(extra or {})
        ex.ladder_template = tpl
        ex._round_meta = {}
        rep = Report()
        sl = mark * 0.92 if "long" in action else mark * 1.08
        leg = Intent(action=action, symbol="ETH_USDT", price=mark * 0.995,
                     size_usd=mark * 0.01, sl=sl)
        ex._complete_ladder([leg], rep)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
        Path(tpl["dynamic"]["db_path"]).unlink(missing_ok=True)
        return c, det

    def test_short_ladder_above_mark(self):
        c, det = self._run("open_short", pos=("short", 3.0))
        prices = [float(b["price"]) for b in c.book]
        sizes = [float(b["size"]) for b in c.book]
        self.assertTrue(all(p > MARK for p in prices), f"空头档必须全在现价上方: {prices}")
        self.assertTrue(all(s < 0 for s in sizes), f"空头挂单 size 必须为负: {sizes}")
        self.assertEqual(len(prices), 10)
        self.assertEqual((det.get("dynamic") or {}).get("side"), "short")

    def test_short_sl_above_highest_and_buy_rule(self):
        c, det = self._run("open_short", pos=("short", 3.0))
        prices = [float(b["price"]) for b in c.book]
        highest = max(prices)
        prot = c.prot[-1]
        sl_px = float(prot["trigger"]["price"])
        self.assertGreater(sl_px, highest, "空头 SL 必须在最高档之上")
        self.assertEqual(int(prot["trigger"]["rule"]), 1, "空头 SL 应是涨破买入")
        self.assertGreater(float(prot["initial"]["size"]), 0, "空头 SL size 应为正")
        # 覆盖 = 空头持仓 3 + 空头挂单 29（空单 left 是负数，早期实现漏算挂单 → 只覆盖 3）
        self.assertEqual(float(prot["initial"]["size"]), 32.0,
                         "空头 SL 必须覆盖 持仓+全部空头挂单")
        adj = (det.get("dynamic") or {}).get("sl_adjusted") or {}
        if adj:
            lo, hi = adj["bounds"]
            self.assertTrue(lo <= sl_px <= hi)

    def test_long_still_below(self):
        c, det = self._run("open_long", pos=("long", 3.0))
        prices = [float(b["price"]) for b in c.book]
        self.assertTrue(all(p < MARK for p in prices))
        self.assertEqual((det.get("dynamic") or {}).get("side"), "long")

    def test_auto_picks_long_when_above_mid(self):
        _c, det = self._run("open_long", extra={"direction_mode": "auto"},
                            dbkw={"bb": MARK * 0.995, "ema20": MARK * 1.002,
                                  "ema50": MARK})
        dyn = det.get("dynamic") or {}
        self.assertEqual(dyn.get("side"), "long")
        self.assertEqual((dyn.get("auto_direction") or {}).get("rule"), "bb_mid+ema")

    def test_auto_picks_short_when_below_mid(self):
        # pos=None：本用例只测「BB 中轨下方 → 判空」，若持多头仓会被
        # 反向持仓护栏拦截（那是另一个用例 test_reverse_guard 的职责）
        c, det = self._run("open_long", pos=None, extra={"direction_mode": "auto"},
                           dbkw={"bb": MARK * 1.02, "ema20": MARK * 0.98,
                                 "ema50": MARK})
        dyn = det.get("dynamic") or {}
        self.assertEqual(dyn.get("side"), "short", "价在 BB 中轨下方应转空头")
        self.assertTrue(all(float(b["price"]) > MARK for b in c.book))


if __name__ == "__main__":
    unittest.main()
