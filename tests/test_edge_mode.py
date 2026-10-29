# -*- coding: utf-8 -*-
"""边缘模式（edge）：只在区间下沿接多 / 上沿接空，中间不挂。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

ATR = 7.5
LO, HI = 2300.0, 2380.0
BARS = 96


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark, pos=("long", 0.0)):
        self.mark, self.pos = mark, pos
        self.book, self.prot = [], []

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
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        return {}


class Report:
    def __init__(self):
        self.results = []


def db(mark):
    """96 根 bar：最低 2300、最高 2380，其余在中间。"""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema20 REAL, ema50 REAL)")
    rows = []
    for i in range(BARS):
        o = 2340.0
        h = HI if i == 5 else 2350.0
        low = LO if i == 40 else 2330.0
        c = 2340.0
        rows.append(("ETH_USDT", "15m", 1793000000 + i * 900, 1, o, h, low, c,
                     ATR, 2340.0, 2350.0, 2330.0, 2340.0, 2340.0))
    con.executemany("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return f.name


EDGE_TPL = {
    "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
    "martin_slots": {3: 6, 7: 7, 10: 9}, "martin": [], "snap_tol_pct": 0.15,
    "direction_mode": "auto",
    "edge": {"enabled": True, "lookback": 96, "levels": 5, "step_atr": 0.5,
             "sl_atr": 2.0, "martin_slots": {1: 9, 3: 7, 5: 6}},
    "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5, "bb_div": 6.0,
                "min_step_pct": 0.45, "max_step_pct": "auto", "max_span_pct": 6.0,
                "scale_martin": True, "max_martin_pct": 9.0},
}


class TestEdgeMode(unittest.TestCase):
    def _run(self, mark, pos=("long", 0.0)):
        c = Client(mark=mark, pos=pos)
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        ex.ladder_template = dict(EDGE_TPL,
                                  dynamic=dict(EDGE_TPL["dynamic"], db_path=db(mark)))
        ex._round_meta = {}
        rep = Report()
        ex._complete_ladder([Intent(action="open_long", symbol="ETH_USDT",
                                    price=mark * 0.995, size_usd=mark * 0.01,
                                    sl=mark * 0.9)], rep)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
        Path(ex.ladder_template["dynamic"]["db_path"]).unlink(missing_ok=True)
        return c, det

    def test_lower_half_goes_long_at_bottom(self):
        """价在区间下半 → 多头档集中在 2300（下沿）附近，中间不挂。"""
        c, det = self._run(2320.0)
        dyn = det.get("dynamic") or {}
        prices = sorted(float(b["price"]) for b in c.book)
        self.assertEqual(dyn.get("side"), "long")
        self.assertGreaterEqual(min(prices), LO - 0.5)
        self.assertLessEqual(max(prices), LO + 5 * 0.5 * ATR + 0.5 + 6,
                             f"档位必须集中在边缘带内: {prices}")
        self.assertLess(len(prices), 10, "边缘模式不应铺满 10 档")
        self.assertLess(max(prices), 2340 - 5, "中间区域不得挂单")

    def test_upper_half_goes_short_at_top(self):
        c, det = self._run(2360.0)
        dyn = det.get("dynamic") or {}
        prices = sorted((float(b["price"]) for b in c.book), reverse=True)
        self.assertEqual(dyn.get("side"), "short")
        self.assertLessEqual(max(prices), HI + 0.5)
        self.assertGreater(min(prices), HI - 5 * 0.5 * ATR - 0.5 - 6)
        self.assertTrue(all(float(b["size"]) < 0 for b in c.book))

    def test_sl_outside_edge(self):
        c, det = self._run(2310.0)
        prot = c.prot[-1]
        sl_px = float(prot["trigger"]["price"])
        self.assertLess(sl_px, LO, "多头 SL 必须在区间下沿之外")
        self.assertEqual(int(prot["trigger"]["rule"]), 2)

    def test_edge_reports_range(self):
        _c, det = self._run(2320.0)
        edge = (det.get("dynamic") or {}).get("edge") or {}
        self.assertAlmostEqual(edge.get("lo"), LO, places=1)
        self.assertAlmostEqual(edge.get("hi"), HI, places=1)
        self.assertEqual(edge.get("bars"), BARS)


if __name__ == "__main__":
    unittest.main()
