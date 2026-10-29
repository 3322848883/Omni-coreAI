# -*- coding: utf-8 -*-
"""马丁占位模型（`martin_slots`）：10 档等距，第 2/6/10 档放大张数。

为什么要这个模型：旧的「网格 + 马丁深度相加」中，马丁档落在网格缝隙里
（缝隙 = step/2 ≈ 0.24%），小于同价容差 0.3% → 必被当成重复吞掉，
实测反复只剩 7~8 层。人格本义是「7 网格 + 3 马丁**穿插**」，即占位而非叠加。
"""
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
    def __init__(self):
        self.book = []
        self.prot = []

    def get_last_price(self, sym):
        return MARK

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

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
        return {}

    def cancel_price_order(self, pid):
        return {}


class Report:
    def __init__(self):
        self.results = []


def _db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, MARK, MARK + 5, MARK - 5, MARK,
                 ATR, MARK, MARK * 1.013, MARK * 0.987))
    con.commit()
    con.close()
    return f.name


class TestMartinSlots(unittest.TestCase):
    def _run(self, sl=2160.0):
        db = _db()
        c = Client()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        ex.ladder_template = {
            "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
            "martin_slots": {2: 6, 6: 7, 10: 9}, "snap_tol_pct": 0.15, "martin": [],
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5,
                        "bb_div": 6.0, "min_step_pct": 0.45, "max_step_pct": "auto",
                        "max_span_pct": 6.0, "scale_martin": True,
                        "max_martin_pct": 9.0, "db_path": db},
        }
        ex._round_meta = {}
        rep = Report()
        leg = Intent(action="open_long", symbol="ETH_USDT", price=MARK * 0.995,
                     size_usd=MARK * 0.01, sl=sl)
        ex._complete_ladder([leg], rep)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
        Path(db).unlink(missing_ok=True)
        rows = sorted(((float(b["price"]), int(float(b["size"]))) for b in c.book),
                      reverse=True)
        return rows, det

    def test_ten_levels_with_slots(self):
        rows, det = self._run()
        self.assertEqual(len(rows), 10, f"必须铺满 10 层，实际 {len(rows)}")
        self.assertEqual([sz for _, sz in rows], [1, 6, 1, 1, 1, 7, 1, 1, 1, 9],
                         "马丁必须落在第 2/6/10 档")
        self.assertEqual((det.get("dynamic") or {}).get("model"), "slots")

    def test_even_spacing(self):
        rows, _ = self._run()
        prices = [p for p, _ in rows]
        gaps = [round(prices[i] - prices[i + 1], 2) for i in range(len(prices) - 1)]
        self.assertTrue(max(gaps) - min(gaps) < 1.0, f"间距应基本等距：{gaps}")

    def test_sl_clamped_into_band(self):
        rows, det = self._run(sl=2160.0)
        deepest = min(p for p, _ in rows)
        adj = (det.get("dynamic") or {}).get("sl_adjusted") or {}
        self.assertTrue(adj, "2160 距最深档过远，必须被拉回")
        lo, hi = adj["bounds"]
        self.assertAlmostEqual(adj["adjusted"], lo, places=1)
        self.assertLessEqual(abs(lo - (deepest - 3 * ATR)), 0.05)
        self.assertGreater(adj["adjusted"], 2160.0)


class TestAlwaysOn(unittest.TestCase):
    """模板常驻：模型 hold（无入场腿）时也要铺满，SL 走 sl_pct 兜底。"""

    def _run(self, intents, tpl_extra=None):
        db = _db()
        c = Client()
        ex = Executor(c, symbols_whitelist=["ETH_USDT"])
        tpl = {
            "enabled": True, "grid_levels": 10, "grid_lots": 1, "grid_step_pct": 0.5,
            "martin_slots": {3: 6, 7: 7, 10: 9}, "martin": [], "snap_tol_pct": 0.15,
            "always_on": True, "symbol": "ETH_USDT", "sl_pct": 5.0,
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5,
                        "bb_div": 6.0, "min_step_pct": 0.45, "max_step_pct": "auto",
                        "max_span_pct": 6.0, "scale_martin": True,
                        "max_martin_pct": 9.0, "db_path": db},
        }
        tpl.update(tpl_extra or {})
        ex.ladder_template = tpl
        ex._round_meta = {}
        rep = Report()
        ex._complete_ladder(intents, rep)
        Path(db).unlink(missing_ok=True)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
        return c, det

    def test_hold_only_still_lays_ladder(self):
        c, _det = self._run([Intent(action="hold", symbol="ETH_USDT")])
        self.assertEqual(len(c.book), 10, "模型 hold 时仍应铺满 10 层")
        self.assertTrue(c.prot, "必须挂出兜底 SL")

    def test_sl_from_config_pct_and_clamped(self):
        c, det = self._run([Intent(action="hold", symbol="ETH_USDT")])
        sl_px = float(c.prot[-1]["trigger"]["price"])
        self.assertLess(sl_px, MARK, "多头 SL 在现价下方")
        adj = (det.get("dynamic") or {}).get("sl_adjusted") or {}
        self.assertTrue(adj, "sl_pct 5% 应被护栏拉进 [最深-3ATR, 最深-1ATR]")

    def test_no_always_on_still_requires_entry(self):
        c, _ = self._run([Intent(action="hold", symbol="ETH_USDT")],
                         {"always_on": False})
        self.assertEqual(c.book, [], "未开常驻时 hold 不铺梯（旧行为）")


if __name__ == "__main__":
    unittest.main()
