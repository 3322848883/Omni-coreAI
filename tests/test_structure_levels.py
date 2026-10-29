# -*- coding: utf-8 -*-
"""结构位档位（不等距）+ 覆盖价距 300~500 点。

用户口径（2026-10-29）：
· 层数动态 10~15，档位放**关键结构位**（OB/FVG/swing），不要等距等比网格
· SL 覆盖价距 300~500 点，区间越大越宽 → 不频繁止损
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omnialpha.config import load_bot_config  # noqa: E402
from omnialpha.executor import Executor  # noqa: E402
from omnialpha.schema import Intent  # noqa: E402

TPL = dict(load_bot_config(ROOT / "config/bots/martin-grid.yaml").ladder_template)
MARK = 2330.0
QUANTO = 0.01


class Meta:
    quanto_multiplier = 0.01
    size_min = 0.1
    size_step = 0.1
    order_price_round = 0.05


class Client:
    def __init__(self):
        self.mark = MARK
        self.book, self.prot = [], []

    def get_last_price(self, sym):
        return self.mark

    def get_contract(self, sym):
        return Meta()

    def get_positions(self):
        return []

    def list_orders(self, sym):
        return [{"id": b["id"], "price": str(b["price"]), "size": b["size"],
                 "left": b["size"], "status": "open"} for b in self.book]

    def list_price_orders(self, sym):
        return self.prot

    def place_order(self, body):
        rec = dict(body, id=f"o{len(self.book)}")
        self.book.append(rec)
        return {"id": rec["id"]}

    def place_price_order(self, body):
        self.prot.append({"id": f"p{len(self.prot)}", "status": "open",
                          "initial": body["initial"], "trigger": body["trigger"]})
        return {"id": f"p{len(self.prot)}"}

    def cancel_order(self, oid):
        self.book = [b for b in self.book if b["id"] != oid]
        return {}

    def cancel_price_order(self, pid):
        self.prot = [p for p in self.prot if p["id"] != pid]
        return {}


def _db(lo, hi, atr=7.5):
    """近 lookback 根的极值由 lo/hi 决定（只插 2 根，min/max 即它们）。"""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    con = sqlite3.connect(f.name)
    con.execute("CREATE TABLE kline (symbol TEXT, interval TEXT, t INTEGER, closed INT, "
                "o REAL, h REAL, l REAL, c REAL, atr14 REAL, boll20 REAL, "
                "boll20_upper REAL, boll20_lower REAL, ema200 REAL, ema20 REAL, ema50 REAL)")
    mid = (lo + hi) / 2
    half = MARK * 2.6 / 200
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "15m", 1793272500, 1, mid, hi, lo, MARK,
                 atr, MARK, MARK + half, MARK - half, None, MARK * .999, MARK * .998))
    con.execute("INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETH_USDT", "4h", 1793246400, 1, MARK, MARK, MARK, MARK,
                 None, None, None, None, 2452.7, None, None))
    con.commit()
    con.close()
    return f.name


def _run(levels, lo=None, hi=None, extra=None):
    lo = lo if lo is not None else MARK - 40
    hi = hi if hi is not None else MARK + 40
    db = _db(lo, hi)
    sd = Path(tempfile.mkdtemp())
    c = Client()
    ex = Executor(c, symbols_whitelist=["ETH_USDT"], ladder_template=dict(TPL))
    t = dict(TPL)
    t["dynamic"] = dict(TPL.get("dynamic") or {}, db_path=db)
    t["anchor_state_file"] = str(sd / "a.json")
    t["direction_mode"] = "signal"          # 只看档位/止损，避开方向
    t.update(extra or {})
    ex.ladder_template = t
    ex._round_meta = {"structure_levels": levels} if levels else {}

    class Rep:
        results = []

    rep = Rep()
    ex._complete_ladder([Intent(action="open_long", symbol="ETH_USDT",
                                price=MARK * 0.99, size_usd=MARK * 0.01,
                                sl=MARK * 0.90)], rep)
    Path(db).unlink(missing_ok=True)
    dets = [r.detail for r in rep.results
            if getattr(r, "action", "") == "ladder_template" and r.detail]
    det = next((d for d in dets if "step_pct" in d or d.get("blocked")), {})
    prices = sorted(float(b["price"]) for b in c.book)
    sizes = [int(float(b["size"])) for b in sorted(c.book, key=lambda x: -float(x["price"]))]
    prot = [p for p in c.prot]
    return prices, sizes, det, prot


class TestStructureLevels(unittest.TestCase):
    def test_uses_structure_levels_not_even_grid(self):
        """给 12 个不均匀结构位 → 档位就用它们（间距不等），不是等距。"""
        lv = [MARK - d for d in (12, 26, 44, 58, 73, 90, 108, 125, 141, 160, 178, 195)]
        prices, sizes, det, _ = _run(lv, lo=MARK - 200, hi=MARK + 40)
        so = (det.get("dynamic") or {}).get("structure_only") or {}
        self.assertTrue(so, f"应走结构位分支，det={det.get('skipped')}")
        self.assertGreaterEqual(len(prices), 10)
        self.assertLessEqual(len(prices), 15)
        gaps = [round(prices[i+1] - prices[i], 1) for i in range(len(prices)-1)]
        self.assertGreater(max(gaps) - min(gaps), 1.0, f"间距应当不等：{gaps}")
        for p in prices:
            self.assertTrue(any(abs(p - x) < 0.02 for x in lv), f"{p} 不在结构位上")

    def test_fills_to_min_levels_when_too_few(self):
        """只给 3 个结构位 → 补到 10 个，且不挤（间距 ≥ min_gap）。"""
        lv = [MARK - 20, MARK - 45, MARK - 70]
        prices, sizes, det, _ = _run(lv, lo=MARK - 300, hi=MARK + 40)
        so = (det.get("dynamic") or {}).get("structure_only") or {}
        self.assertEqual(len(prices), 10, f"应补到 10，实际 {len(prices)}")
        self.assertEqual(so.get("filled"), 7)
        gaps = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
        self.assertGreaterEqual(min(gaps) / MARK * 100, 0.29, f"补点不能挤：{gaps}")

    def test_no_structure_falls_back_to_even(self):
        """完全没有结构位 → 回退等距 10 层（不留空盘、也不凭空造结构位）。"""
        prices, sizes, det, _ = _run([], lo=MARK - 40, hi=MARK + 40)
        self.assertEqual(len(prices), 10, "应回退等距 10 层")
        dyn = det.get("dynamic") or {}
        self.assertNotIn("structure_only", dyn)
        gaps = [round(prices[i+1] - prices[i], 2) for i in range(len(prices)-1)]
        self.assertLess(max(gaps) - min(gaps), 0.5, f"回退后应是等距：{gaps}")

    def test_martin_lots_follow_rank(self):
        """张数按**位次比例**（3/7/10 @ 10 层 = 30%/70%/100%），层数变了位置跟着走。"""
        lv = [MARK - 10 * k for k in range(1, 13)]
        prices, sizes, det, _ = _run(lv, lo=MARK - 200, hi=MARK + 40)
        n = len(prices)
        self.assertEqual(n, 12, f"12 个结构位应全用，实际 {n}")
        want = {max(1, round(0.3 * n)): 6, max(1, round(0.7 * n)): 7, n: 9}
        for idx, lots in want.items():
            self.assertEqual(sizes[idx-1], lots, f"第 {idx} 档应 {lots} 张，实际 {sizes}")

    def test_sl_points_300_when_range_small(self):
        """24h 区间 60 点 → ×4 = 240 → 取下限 300 点。"""
        lv = [MARK - 10 * k for k in range(1, 15)]
        prices, _sizes, det, prot = _run(lv, lo=MARK - 30, hi=MARK + 30)
        sl = (det.get("dynamic") or {}).get("sl_points") or {}
        self.assertEqual(sl.get("mode"), "points")
        self.assertAlmostEqual(sl.get("points"), 300.0, places=1)
        deepest = min(prices)
        rule2 = [p for p in prot if int((p.get("trigger") or {}).get("rule") or 0) == 2]
        self.assertTrue(rule2)
        trig = float(rule2[-1]["trigger"]["price"])
        self.assertAlmostEqual(deepest - trig, 300.0, delta=1.0,
                               msg=f"SL 应在最深档下方 300 点，实际 {deepest-trig:.1f}")

    def test_sl_points_scales_with_range(self):
        """24h 区间 150 点 → ×4 = 600 → 钳到上限 500。"""
        lv = [MARK - 10 * k for k in range(1, 15)]
        _p, _s, det, _prot = _run(lv, lo=MARK - 75, hi=MARK + 75)
        sl = (det.get("dynamic") or {}).get("sl_points") or {}
        self.assertAlmostEqual(sl.get("points"), 500.0, places=1,
                               msg=f"区间 150 点应到上限 500，实际 {sl.get('points')}")

    def test_levels_capped_at_15(self):
        """给 25 个结构位 → 最多取 15 个。"""
        lv = [MARK - 8 * k for k in range(1, 26)]
        prices, _s, _d, _p = _run(lv, lo=MARK - 250, hi=MARK + 40)
        self.assertLessEqual(len(prices), 15)


if __name__ == "__main__":
    unittest.main()
