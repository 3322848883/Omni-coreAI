# -*- coding: utf-8 -*-
"""锚点模式（P0）：fixed 跨轮保持 + 重锚阈值 + state 持久化。"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self, mark):
        self.mark = mark
        self.book, self.total = [], 0

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open", "text": "t-mtg"} for b in self.book]

    def list_price_orders(self, sym):
        return []

    def place_order(self, body):
        rec = dict(body, id=f"o{self.total}")
        self.total += 1
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        return {"id": "p"}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        return {}


class Report:
    def __init__(self):
        self.results = []


def _db(mark):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL)")
    half = mark * 0.013
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793170000, 1, mark, mark + 5, mark - 5, mark,
                 mark * 0.0033, mark, mark + half, mark - half))
    con.commit()
    con.close()
    return f.name


class TestAnchor(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / "anchor.json"
        self.client = Client(2340.0)
        self.ex = Executor(self.client, symbols_whitelist=["ETH_USDT"])

    def _tpl(self, mark, mode="fixed", thr=2.0):
        return {
            "enabled": True, "grid_levels": 7, "grid_lots": 1, "grid_step_pct": 0.5,
            "anchor_mode": mode, "reanchor_threshold_pct": thr,
            "anchor_state_file": str(self.state),
            "dynamic": {"enabled": True, "interval": "15m", "atr_mult": 1.5,
                        "bb_div": 6.0, "min_step_pct": 0.45, "max_step_pct": "auto",
                        "max_span_pct": 6.0, "scale_martin": True,
                        "max_martin_pct": 9.0, "db_path": _db(mark)},
            "martin": [{"pct": 4.2, "lots": 6}, {"pct": 5.4, "lots": 7},
                       {"pct": 6.6, "lots": 9}],
        }

    def _run(self, mark, mode="fixed", thr=2.0):
        self.client.mark = mark
        self.ex.ladder_template = self._tpl(mark, mode, thr)
        rep = Report()
        leg = Intent(action="open_long", symbol="ETH_USDT", price=mark * 0.995,
                     size_usd=mark * 0.01, sl=mark * 0.92)
        before = self.client.total
        self.ex._complete_ladder([leg], rep)
        dets = [s.detail for s in rep.results
                if getattr(s, "action", "") == "ladder_template" and s.detail]
        det = next((d for d in dets if "step_pct" in d), dets[0] if dets else {})
        return self.client.total - before, det

    def test_first_round_writes_state(self):
        added, det = self._run(2340.0)
        self.assertEqual(added, 10)
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertAlmostEqual(data["ETH_USDT"]["anchor"], 2340.0, places=2)
        self.assertTrue((det.get("anchor_info") or {}).get("reanchored"))

    def test_second_round_keeps_book_untouched(self):
        self._run(2340.0)
        added, det = self._run(2341.0)      # drift 0.04% 未越阈值
        self.assertEqual(added, 0, "锚点未漂移时不得重铺")
        self.assertEqual(det.get("skipped"), "already_full")
        self.assertAlmostEqual(det.get("anchor"), 2340.0, places=2)

    def test_drift_below_threshold_no_reanchor(self):
        self._run(2340.0)
        added, det = self._run(2384.0)      # +1.88%
        self.assertEqual(added, 0)
        self.assertFalse((det.get("anchor_info") or {}).get("reanchored"))

    def test_drift_over_threshold_reanchors(self):
        self._run(2340.0)
        added, det = self._run(2390.0)      # +2.14%
        info = det.get("anchor_info") or {}
        self.assertTrue(info.get("reanchored"))
        self.assertAlmostEqual(det.get("anchor"), 2390.0, places=1)
        self.assertGreater(added, 0, "重锚后必须补新档")
        self.assertEqual(len(self.client.book), 10, "重锚后盘面仍应是 10 层")

    def test_trailing_does_not_write_state(self):
        self._run(2340.0, mode="trailing")
        self.assertFalse(self.state.exists(), "trailing 模式不得落盘锚点")


if __name__ == "__main__":
    unittest.main()
